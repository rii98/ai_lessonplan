"""AdminService — the owner's operations, behind ONE application service shared by
the owner console (HTTP) and ``lessonforge-admin`` (CLI).

Every mutation is audit-logged with the acting :class:`Actor`, whichever door it
came through, and guarded the same way:

- you cannot disable or delete your own account (no self-lockout);
- a superadmin cannot be disabled or deleted from the console — their grant is a
  CLI-only boundary (D5), so removing one goes through the CLI too;
- deleting someone's data requires typing their subject back (``confirm``).

Superadmin grants are READ-ONLY here (D5): :meth:`grants` lists them; granting
and revoking stay in the CLI, which needs database access.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

from ..audit import AuditEntry
from ..auth.directory import PeopleFilter, PrincipalRecord
from ..auth.principal import SUPERADMIN, Principal
from ..knowledge.scope import GLOBAL_SPACE, AccessScope
from ..pagination import Keyset, Page
from ..platform import Platform
from ..providers.base import LLMClient, VectorStore
from ..rag.documents import Collection


class AdminError(ValueError):
    """An owner operation that is refused (→ 409 conflict)."""


@dataclass(frozen=True, slots=True)
class Actor:
    label: str                       # "edboox:teacher:1" or "cli:<os user>"
    principal_id: str | None = None

    @classmethod
    def of(cls, p: Principal) -> Actor:
        return cls(f"{p.issuer}:{p.subject}", p.id)


def person_view(r: PrincipalRecord) -> dict[str, Any]:
    return {"id": r.id, "issuer": r.issuer, "subject": r.subject, "name": r.display_name,
            "role": r.external_role, "superadmin": SUPERADMIN in r.grants,
            "grants": sorted(r.grants), "school": r.tenant_id, "disabled": r.disabled,
            "created_at": r.created_at, "last_seen_at": r.last_seen_at,
            "context_synced_at": r.context_synced_at}


class AdminService:
    def __init__(self, platform: Platform, *, vector_store: VectorStore | None = None,
                 llm: LLMClient | None = None,
                 counts: Callable[[], dict[str, dict[str, int]]] | None = None) -> None:
        self.p = platform
        self.vector_store = vector_store
        self.llm = llm
        self._counts = counts or dict

    # ── audit helper ────────────────────────────────────────────────────────
    def _audit(self, actor: Actor, action: str, target: str, **detail: Any) -> None:
        self.p.audit.record(AuditEntry(actor=actor.label, action=action, target=target,
                                       detail=detail, principal_id=actor.principal_id))

    def _forget_cached_auth(self) -> None:
        if "auth" in self.p.__dict__:          # only if the auth service was built
            self.p.auth.invalidate()

    # ── overview ────────────────────────────────────────────────────────────
    def overview(self) -> dict[str, Any]:
        from ..db.migrate import discover, status

        health: dict[str, Any] = {}
        if self.llm is not None:
            health["llm"] = _safe(self.llm.health)
        if self.vector_store is not None:
            health["vector_store"] = _safe(self.vector_store.health)
        db = self.p.db
        health["database"] = db.health() if db is not None else True
        schema: dict[str, Any] = {"backend": "postgres" if db else "memory"}
        if db is not None:
            try:
                with db.tx() as cur:
                    from ..db.migrate import _CursorConn

                    st = status(_CursorConn(cur))
                schema.update(current=st.current, applied=st.applied,
                              pending=[f"{m.version:04d}_{m.name}" for m in st.pending],
                              drifted=st.drifted)
            except Exception as exc:
                schema.update(current=False, error=str(exc))
        else:
            schema.update(current=True, applied=[m.version for m in discover()])
        corpus = {}
        if self.vector_store is not None:
            for col in Collection:
                corpus[col.value] = _safe(lambda c=col: self.vector_store.count(
                    c.value, scope=AccessScope.all()), default=None)
        edboox = (_safe(self.p.verifier.key_status, default=[])
                  if self.p.settings.auth.issuers else [])
        return {"health": health, "schema": schema, "people": self.p.directory.stats(),
                "content": self._counts(), "corpus": corpus, "edboox": edboox,
                "settings": {"context_provider": self.p.settings.context.provider,
                             "activity_provider": self.p.settings.activity.provider,
                             "retention_days": self.p.settings.activity.retention_days,
                             "scope_modes": {r: c.mode for r, c in
                                             self.p.settings.scope.roles.items()}}}

    # ── people ──────────────────────────────────────────────────────────────
    def people(self, f: PeopleFilter, *, limit: int, cursor: Keyset | None) -> Page[dict]:
        page = self.p.directory.search(f, limit=limit, cursor=cursor)
        return Page([person_view(r) for r in page.items], page.next_cursor)

    def _record(self, principal_id: str) -> PrincipalRecord:
        rec = self.p.directory.get(principal_id)
        if rec is None:
            raise LookupError(principal_id)
        return rec

    def person(self, principal_id: str) -> dict[str, Any]:
        rec = self._record(principal_id)
        return {**person_view(rec), "context": rec.context or None}

    def _guard(self, rec: PrincipalRecord, actor: Actor, verb: str) -> None:
        if actor.principal_id == rec.id:
            raise AdminError(f"you cannot {verb} your own account")
        if SUPERADMIN in rec.grants:
            raise AdminError(f"cannot {verb} a superadmin here — revoke the grant with "
                             f"lessonforge-admin first (D5)")

    def set_disabled(self, principal_id: str, disabled: bool, actor: Actor) -> dict[str, Any]:
        rec = self._record(principal_id)
        if disabled:
            self._guard(rec, actor, "disable")
        self.p.directory.set_disabled(rec.issuer, rec.subject, disabled)
        self._forget_cached_auth()
        self._audit(actor, "principal.disable" if disabled else "principal.enable",
                    f"{rec.issuer}:{rec.subject}")
        return self.person(principal_id)

    def forget(self, principal_id: str, confirm: str, actor: Actor) -> None:
        rec = self._record(principal_id)
        self._guard(rec, actor, "delete")
        if confirm != rec.subject:
            raise AdminError(f"type the subject ({rec.subject}) to confirm")
        self.p.directory.delete(rec.issuer, rec.subject)
        self._forget_cached_auth()
        self._audit(actor, "principal.forget", f"{rec.issuer}:{rec.subject}")

    def sync_context(self, principal_id: str, actor: Actor) -> dict[str, Any]:
        rec = self._record(principal_id)
        p = Principal(id=rec.id, issuer=rec.issuer, subject=rec.subject,
                      role=rec.external_role, roles=frozenset({rec.external_role}),
                      permissions=frozenset(), tenant_id=rec.tenant_id,
                      display_name=rec.display_name)
        ctx = self.p.context.refresh(p)
        self._audit(actor, "principal.sync_context", f"{rec.issuer}:{rec.subject}",
                    refreshed=ctx is not None)
        return {"refreshed": ctx is not None,
                "context": ctx.model_dump(mode="json") if ctx else rec.context or None}

    def grants(self) -> list[dict[str, Any]]:
        return [asdict(g) for g in self.p.directory.grants()]

    # ── audit log ───────────────────────────────────────────────────────────
    def audit_log(self, *, limit: int, before: int | None, action: str | None) -> dict[str, Any]:
        rows = self.p.audit.recent(limit + 1, before=before, action=action)
        items = [asdict(e) for e in rows[:limit]]
        return {"items": items,
                "next_cursor": str(rows[limit - 1].id) if len(rows) > limit and rows[limit - 1].id
                else None}

    # ── maintenance ─────────────────────────────────────────────────────────
    def purge(self, actor: Actor) -> dict[str, int]:
        out = {"launch_tokens": self.p.replay.purge(),
               "activity_events": self.p.activity_reader.purge_older_than(
                   self.p.settings.activity.retention_days)}
        self._audit(actor, "maintenance.purge", "platform", **out)
        return out

    def backfill_spaces(self, actor: Actor) -> dict[str, int]:
        if self.vector_store is None:
            raise AdminError("no vector store is configured")
        out = {col.value: self.vector_store.backfill_space(col.value, GLOBAL_SPACE)
               for col in Collection}
        self._audit(actor, "corpus.backfill_spaces", "vector_store", points=sum(
            max(n, 0) for n in out.values()))
        return out


def _safe(fn: Callable[[], Any], default: Any = False) -> Any:
    try:
        return fn()
    except Exception:
        return default
