"""PrincipalDirectory — maps ``(issuer, subject)`` to our principal and its grants.

LessonForge keeps no user directory of its own (Edboox is the system of record);
a principal row exists so our data has a stable owner UUID, so local grants
(superadmin, D5) have something to attach to, and so cached user context has a
home. Rows are upserted from verified tokens; grants are written only by the
admin CLI.

Postgres does the token upsert AND reads the grants in ONE round trip.
"""

from __future__ import annotations

import threading
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

from ..db.pool import Database, as_uuid, contains_pattern, id_str, jsonb
from ..pagination import Keyset, Page, page_of
from .verifier import VerifiedToken

GRANTABLE_ROLES = frozenset({"superadmin"})


@dataclass(frozen=True, slots=True)
class PrincipalRecord:
    id: str
    issuer: str
    subject: str
    external_role: str
    display_name: str | None = None
    tenant_id: str | None = None
    grants: frozenset[str] = frozenset()
    disabled: bool = False
    context: dict[str, Any] = field(default_factory=dict, compare=False, hash=False)
    context_synced_at: datetime | None = None
    created_at: datetime | None = None
    last_seen_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class PeopleFilter:
    """Owner-console search over principals."""

    q: str | None = None             # subject or name contains
    role: str | None = None          # external role, or "superadmin" (has the grant)
    tenant_id: str | None = None
    disabled: bool | None = None


def _seen_key(r: PrincipalRecord) -> Keyset:
    return Keyset(r.last_seen_at or datetime.min.replace(tzinfo=UTC), r.id)


@dataclass(frozen=True, slots=True)
class GrantRow:
    issuer: str
    subject: str
    role: str
    granted_by: str
    granted_at: datetime


class PrincipalDirectory(ABC):
    @abstractmethod
    def upsert(self, token: VerifiedToken) -> PrincipalRecord:
        """Create or refresh the principal for a verified token (role, name and
        tenant follow the latest token) and return it with its local grants."""

    @abstractmethod
    def get(self, principal_id: str) -> PrincipalRecord | None: ...

    @abstractmethod
    def find(self, issuer: str, subject: str) -> PrincipalRecord | None: ...

    @abstractmethod
    def save_context(self, principal_id: str, context: dict[str, Any]) -> None:
        """Store refreshed user context (our vocabulary) and stamp the sync time."""

    @abstractmethod
    def grant(self, issuer: str, subject: str, role: str, *, granted_by: str) -> bool:
        """Add a local grant (creating a placeholder principal if they have never
        logged in). Returns ``False`` if it already existed."""

    @abstractmethod
    def revoke(self, issuer: str, subject: str, role: str) -> bool: ...

    @abstractmethod
    def grants(self) -> list[GrantRow]: ...

    @abstractmethod
    def set_disabled(self, issuer: str, subject: str, disabled: bool) -> bool: ...

    @abstractmethod
    def delete(self, issuer: str, subject: str) -> bool:
        """Forget a principal: their documents, chats and profile cascade; their
        activity events are anonymised (enforced by a database trigger)."""

    @abstractmethod
    def search(self, f: PeopleFilter, *, limit: int,
               cursor: Keyset | None = None) -> Page[PrincipalRecord]:
        """Principals, most recently seen first (keyset-paged) — the owner console."""

    @abstractmethod
    def stats(self) -> dict[str, int]:
        """Counts for the owner console: total, per external role, superadmins, disabled."""


def _check_role(role: str) -> None:
    if role not in GRANTABLE_ROLES:
        raise ValueError(f"role {role!r} cannot be granted locally (allowed: {sorted(GRANTABLE_ROLES)})")


# ── memory ───────────────────────────────────────────────────────────────────
class MemoryPrincipalDirectory(PrincipalDirectory):
    def __init__(self) -> None:
        self._by_key: dict[tuple[str, str], PrincipalRecord] = {}
        self._by_id: dict[str, tuple[str, str]] = {}
        self._grants: dict[tuple[str, str], dict[str, GrantRow]] = {}
        self._lock = threading.Lock()

    def _with_grants(self, rec: PrincipalRecord) -> PrincipalRecord:
        return replace(rec, grants=frozenset(self._grants.get((rec.issuer, rec.subject), {})))

    def upsert(self, token: VerifiedToken) -> PrincipalRecord:
        key = (token.issuer, token.subject)
        with self._lock:
            old = self._by_key.get(key)
            rec = PrincipalRecord(
                id=old.id if old else str(uuid.uuid4()),
                issuer=token.issuer, subject=token.subject, external_role=token.role,
                display_name=token.display_name or (old.display_name if old else None),
                tenant_id=token.tenant_id,
                disabled=old.disabled if old else False,
                context=old.context if old else {},
                context_synced_at=old.context_synced_at if old else None,
                created_at=old.created_at if old and old.created_at else datetime.now(UTC),
                last_seen_at=datetime.now(UTC),
            )
            self._by_key[key] = rec
            self._by_id[rec.id] = key
            return self._with_grants(rec)

    def get(self, principal_id: str) -> PrincipalRecord | None:
        key = self._by_id.get(principal_id)
        return self._with_grants(self._by_key[key]) if key else None

    def find(self, issuer: str, subject: str) -> PrincipalRecord | None:
        rec = self._by_key.get((issuer, subject))
        return self._with_grants(rec) if rec else None

    def save_context(self, principal_id: str, context: dict[str, Any]) -> None:
        with self._lock:
            key = self._by_id.get(principal_id)
            if key:
                self._by_key[key] = replace(self._by_key[key], context=dict(context),
                                            context_synced_at=datetime.now(UTC))

    def grant(self, issuer: str, subject: str, role: str, *, granted_by: str) -> bool:
        _check_role(role)
        with self._lock:
            if (issuer, subject) not in self._by_key:
                now = datetime.now(UTC)
                rec = PrincipalRecord(id=str(uuid.uuid4()), issuer=issuer, subject=subject,
                                      external_role="pending", created_at=now, last_seen_at=now)
                self._by_key[(issuer, subject)] = rec
                self._by_id[rec.id] = (issuer, subject)
            roles = self._grants.setdefault((issuer, subject), {})
            if role in roles:
                return False
            roles[role] = GrantRow(issuer, subject, role, granted_by, datetime.now(UTC))
            return True

    def revoke(self, issuer: str, subject: str, role: str) -> bool:
        with self._lock:
            return self._grants.get((issuer, subject), {}).pop(role, None) is not None

    def grants(self) -> list[GrantRow]:
        return sorted((g for roles in self._grants.values() for g in roles.values()),
                      key=lambda g: (g.issuer, g.subject, g.role))

    def set_disabled(self, issuer: str, subject: str, disabled: bool) -> bool:
        with self._lock:
            rec = self._by_key.get((issuer, subject))
            if rec is None:
                return False
            self._by_key[(issuer, subject)] = replace(rec, disabled=disabled)
            return True

    def delete(self, issuer: str, subject: str) -> bool:
        with self._lock:
            rec = self._by_key.pop((issuer, subject), None)
            if rec is None:
                return False
            self._by_id.pop(rec.id, None)
            self._grants.pop((issuer, subject), None)
            return True

    def search(self, f: PeopleFilter, *, limit: int, cursor: Keyset | None = None):
        needle = (f.q or "").casefold()
        rows = []
        for rec in (self._with_grants(r) for r in self._by_key.values()):
            if needle and needle not in rec.subject.casefold() and needle not in (
                    rec.display_name or "").casefold():
                continue
            if f.role == "superadmin" and "superadmin" not in rec.grants:
                continue
            if f.role not in (None, "superadmin") and rec.external_role != f.role:
                continue
            if f.tenant_id is not None and rec.tenant_id != f.tenant_id:
                continue
            if f.disabled is not None and rec.disabled != f.disabled:
                continue
            rows.append(rec)
        rows.sort(key=lambda r: (_seen_key(r).ts, r.id), reverse=True)
        if cursor is not None:
            rows = [r for r in rows if cursor.before(_seen_key(r).ts, r.id)]
        return page_of(rows[:limit + 1], limit, _seen_key)

    def stats(self) -> dict[str, int]:
        recs = [self._with_grants(r) for r in self._by_key.values()]
        out = {"total": len(recs), "superadmins": sum("superadmin" in r.grants for r in recs),
               "disabled": sum(r.disabled for r in recs)}
        for r in recs:
            out[f"role:{r.external_role}"] = out.get(f"role:{r.external_role}", 0) + 1
        return out


# ── postgres ─────────────────────────────────────────────────────────────────
_COLS = ("p.id, p.issuer, p.subject, p.external_role, p.display_name, p.tenant_id, "
         "p.disabled_at, p.context, p.context_synced_at, p.created_at, p.last_seen_at")
_GRANTS = ("COALESCE((SELECT array_agg(g.role) FROM role_grants g "
           "WHERE g.principal_id = p.id), '{}') AS grants")


def _record(r: dict[str, Any]) -> PrincipalRecord:
    return PrincipalRecord(
        id=id_str(r["id"]) or "", issuer=r["issuer"], subject=r["subject"],
        external_role=r["external_role"], display_name=r["display_name"],
        tenant_id=r["tenant_id"], grants=frozenset(r.get("grants") or ()),
        disabled=r["disabled_at"] is not None, context=r["context"] or {},
        context_synced_at=r["context_synced_at"], created_at=r.get("created_at"),
        last_seen_at=r.get("last_seen_at"),
    )


class PostgresPrincipalDirectory(PrincipalDirectory):
    def __init__(self, db: Database) -> None:
        self.db = db

    def upsert(self, token: VerifiedToken) -> PrincipalRecord:
        with self.db.tx() as cur:
            cur.execute(
                f"""WITH p AS (
                      INSERT INTO principals (issuer, subject, external_role, display_name, tenant_id)
                      VALUES (%s, %s, %s, %s, %s)
                      ON CONFLICT (issuer, subject) DO UPDATE SET
                        external_role = EXCLUDED.external_role,
                        display_name  = COALESCE(EXCLUDED.display_name, principals.display_name),
                        tenant_id     = EXCLUDED.tenant_id,
                        last_seen_at  = now()
                      RETURNING *)
                    SELECT {_COLS}, {_GRANTS} FROM p""",
                (token.issuer, token.subject, token.role, token.display_name, token.tenant_id),
            )
            return _record(cur.fetchone())

    def get(self, principal_id: str) -> PrincipalRecord | None:
        pid = as_uuid(principal_id)
        if pid is None:
            return None
        with self.db.tx() as cur:
            cur.execute(f"SELECT {_COLS}, {_GRANTS} FROM principals p WHERE p.id = %s", (pid,))
            row = cur.fetchone()
        return _record(row) if row else None

    def find(self, issuer: str, subject: str) -> PrincipalRecord | None:
        with self.db.tx() as cur:
            cur.execute(f"SELECT {_COLS}, {_GRANTS} FROM principals p "
                        "WHERE p.issuer = %s AND p.subject = %s", (issuer, subject))
            row = cur.fetchone()
        return _record(row) if row else None

    def save_context(self, principal_id: str, context: dict[str, Any]) -> None:
        pid = as_uuid(principal_id)
        if pid is None:
            return
        with self.db.tx() as cur:
            cur.execute("UPDATE principals SET context = %s, context_synced_at = now() "
                        "WHERE id = %s", (jsonb(context), pid))

    def grant(self, issuer: str, subject: str, role: str, *, granted_by: str) -> bool:
        _check_role(role)
        with self.db.tx() as cur:
            cur.execute(
                """WITH p AS (
                     INSERT INTO principals (issuer, subject, external_role)
                     VALUES (%s, %s, 'pending')
                     ON CONFLICT (issuer, subject) DO UPDATE SET issuer = EXCLUDED.issuer
                     RETURNING id)
                   INSERT INTO role_grants (principal_id, role, granted_by)
                   SELECT id, %s, %s FROM p
                   ON CONFLICT (principal_id, role) DO NOTHING
                   RETURNING 1""",
                (issuer, subject, role, granted_by),
            )
            return cur.fetchone() is not None

    def revoke(self, issuer: str, subject: str, role: str) -> bool:
        with self.db.tx() as cur:
            cur.execute(
                "DELETE FROM role_grants g USING principals p WHERE g.principal_id = p.id "
                "AND p.issuer = %s AND p.subject = %s AND g.role = %s RETURNING 1",
                (issuer, subject, role),
            )
            return cur.fetchone() is not None

    def grants(self) -> list[GrantRow]:
        with self.db.tx() as cur:
            cur.execute("SELECT p.issuer, p.subject, g.role, g.granted_by, g.granted_at "
                        "FROM role_grants g JOIN principals p ON p.id = g.principal_id "
                        "ORDER BY p.issuer, p.subject, g.role")
            return [GrantRow(r["issuer"], r["subject"], r["role"], r["granted_by"],
                             r["granted_at"]) for r in cur.fetchall()]

    def set_disabled(self, issuer: str, subject: str, disabled: bool) -> bool:
        with self.db.tx() as cur:
            cur.execute("UPDATE principals SET disabled_at = CASE WHEN %s THEN now() END "
                        "WHERE issuer = %s AND subject = %s RETURNING 1",
                        (disabled, issuer, subject))
            return cur.fetchone() is not None

    def delete(self, issuer: str, subject: str) -> bool:
        with self.db.tx() as cur:
            cur.execute("DELETE FROM principals WHERE issuer = %s AND subject = %s RETURNING 1",
                        (issuer, subject))
            return cur.fetchone() is not None

    def search(self, f: PeopleFilter, *, limit: int, cursor: Keyset | None = None):
        sql, params = [], []
        if f.q:
            like = contains_pattern(f.q)
            sql.append("(p.subject ILIKE %s OR p.display_name ILIKE %s)")
            params += [like, like]
        if f.role == "superadmin":
            sql.append("EXISTS (SELECT 1 FROM role_grants g WHERE g.principal_id = p.id "
                       "AND g.role = 'superadmin')")
        elif f.role is not None:
            sql.append("p.external_role = %s")
            params.append(f.role)
        if f.tenant_id is not None:
            sql.append("p.tenant_id = %s")
            params.append(f.tenant_id)
        if f.disabled is not None:
            sql.append("p.disabled_at IS NOT NULL" if f.disabled else "p.disabled_at IS NULL")
        if cursor is not None:
            sql.append("(p.last_seen_at, p.id) < (%s, %s)")
            params += [cursor.ts, as_uuid(cursor.id)]
        where = f"WHERE {' AND '.join(sql)}" if sql else ""
        with self.db.tx() as cur:
            cur.execute(f"SELECT {_COLS}, {_GRANTS} FROM principals p {where} "
                        "ORDER BY p.last_seen_at DESC, p.id DESC LIMIT %s", [*params, limit + 1])
            rows = [_record(r) for r in cur.fetchall()]
        return page_of(rows, limit, _seen_key)

    def stats(self) -> dict[str, int]:
        with self.db.tx() as cur:
            cur.execute("""SELECT count(*) AS total,
                                  count(*) FILTER (WHERE disabled_at IS NOT NULL) AS disabled,
                                  (SELECT count(*) FROM role_grants
                                    WHERE role = 'superadmin') AS superadmins
                             FROM principals""")
            out = dict(cur.fetchone())
            cur.execute("SELECT external_role, count(*) AS n FROM principals GROUP BY 1")
            out.update({f"role:{r['external_role']}": r["n"] for r in cur.fetchall()})
        return {k: int(v) for k, v in out.items()}

