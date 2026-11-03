"""MemoryStore — owner-isolated persistence for memory items, mastery, the per-user switch
and the profile digest. Every read and write is keyed by ``owner_id``; there is no
cross-user query (retention is the one global sweep, and it reads no content).

**One write algorithm, two backends.** :meth:`MemoryStore.apply` is the only way the
reconciled ops reach storage. It runs inside a per-owner *unit of work* — a threading lock
(``memory``) or one transaction holding a per-owner advisory lock (``postgres``, so two
replicas cannot interleave) — and it **re-validates every op against the live set at
apply time**: the LLM decided on a snapshot that may be seconds old, so an update whose
target was meanwhile replaced is dropped rather than resurrecting stale content, and an
add that duplicates something now live is a no-op. This is optimistic concurrency: no
lock is ever held across an LLM call.

At the per-learner cap the store does not freeze: an add evicts the least important,
least recently used item — but only one that is no more important than the newcomer,
and never something the learner explicitly asked us to remember.

Every change bumps the learner's ``version`` (and ``removal_version`` when something is
removed or rewritten) — see :class:`~lessonforge.domain.memory.MemoryState`.

Similarity search is per user over a small capped set, so recall scores a cached,
pre-normalised matrix in process (see ``recall.py``) — no vector extension. The port
allows a pgvector/Qdrant implementation later.
"""

from __future__ import annotations

import copy
import threading
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from ...db.pool import Database, as_uuid, id_str, jsonb
from ...domain.memory import (
    ApplyResult,
    MasteryRecord,
    MemoryItem,
    MemoryState,
    StoredProfile,
)


def _now() -> datetime:
    return datetime.now(UTC)


def norm(text: str) -> str:
    return " ".join(text.casefold().split()).rstrip(".!")


@dataclass(slots=True)
class Change:
    """A reconciled, policy-approved op ready to apply. ``item`` is the full new row for
    add/update (embedding already computed); ``target_id`` names the row an update or
    retire acts on."""

    op: str                       # add | update | retire
    item: MemoryItem | None = None
    target_id: str | None = None


_ORDER = {"retire": 0, "update": 1, "add": 2}   # removals first: they free capacity


class _Unit(ABC):
    """The primitives :meth:`MemoryStore.apply` needs, inside one locked unit of work."""

    @abstractmethod
    def live(self) -> list[MemoryItem]: ...

    @abstractmethod
    def insert(self, item: MemoryItem) -> None: ...

    @abstractmethod
    def end(self, item_id: str, reason: str, superseded_by: str | None = None) -> None: ...

    @abstractmethod
    def bump(self, removal: bool) -> None: ...


class MemoryStore(ABC):
    # ── the write path ───────────────────────────────────────────────────────
    @abstractmethod
    def _unit(self, owner_id: str) -> AbstractContextManager[_Unit]:
        """A locked unit of work for one owner (all or nothing)."""

    def apply(self, owner_id: str, changes: list[Change], *, max_items: int = 300) -> ApplyResult:
        res = ApplyResult()
        if not changes:
            return res
        with self._unit(owner_id) as u:
            now = _now()
            live = {i.id: i for i in u.live()}
            for ch in sorted(changes, key=lambda c: _ORDER.get(c.op, 9)):
                if ch.op == "retire":
                    if ch.target_id in live:
                        u.end(ch.target_id, "retired")
                        live.pop(ch.target_id)
                        res.retired.append(ch.target_id)
                    else:
                        res.skipped += 1
                elif ch.op == "update" and ch.item is not None:
                    old = live.get(ch.target_id or "")
                    if old is None:          # replaced/forgotten since the decision: stale
                        res.skipped += 1
                        continue
                    new = ch.item
                    if norm(new.content) == norm(old.content):
                        res.skipped += 1     # same fact, nothing to rewrite
                        continue
                    new.owner_id = owner_id
                    new.importance = max(new.importance, old.importance)
                    if old.source.get("explicit"):
                        new.source = {**new.source, "explicit": True}
                    u.insert(new)
                    u.end(old.id, "superseded", superseded_by=new.id)
                    live.pop(old.id)
                    live[new.id] = new
                    res.updated.append(new)
                elif ch.op == "add" and ch.item is not None:
                    new = ch.item
                    new.owner_id = owner_id
                    if any(norm(i.content) == norm(new.content) for i in live.values()):
                        res.skipped += 1
                        continue
                    if len(live) >= max_items:
                        victim = _eviction_victim(live.values(), now)
                        if victim is None or victim.importance > new.importance:
                            res.skipped += 1
                            continue
                        u.end(victim.id, "evicted")
                        live.pop(victim.id)
                        res.evicted.append(victim.id)
                    u.insert(new)
                    live[new.id] = new
                    res.added.append(new)
                else:
                    res.skipped += 1
            if res.changed:
                u.bump(removal=bool(res.updated or res.retired or res.evicted))
        return res

    # ── convenience writes (direct, still versioned) ─────────────────────────
    def add(self, item: MemoryItem) -> MemoryItem:
        """Insert unconditionally (code-authored rows, e.g. lesson episodes)."""
        with self._unit(item.owner_id) as u:
            u.insert(item)
            u.bump(removal=False)
        return item

    def supersede(self, owner_id: str, old_id: str, new: MemoryItem) -> MemoryItem | None:
        """Replace an item, keeping its history. ``None`` if ``old_id`` isn't live and the owner's."""
        with self._unit(owner_id) as u:
            if old_id not in {i.id for i in u.live()}:
                return None
            new.owner_id = owner_id
            u.insert(new)
            u.end(old_id, "superseded", superseded_by=new.id)
            u.bump(removal=True)
        return new

    # ── reads ────────────────────────────────────────────────────────────────
    @abstractmethod
    def live(self, owner_id: str) -> list[MemoryItem]:
        """Current (not ended, not expired) items, newest first."""

    @abstractmethod
    def state(self, owner_id: str) -> MemoryState:
        """Switch + generation counters + stored digest, in one lookup."""

    # ── the learner's controls ───────────────────────────────────────────────
    @abstractmethod
    def delete(self, owner_id: str, item_id: str) -> bool:
        """HARD delete (the learner's "forget"), including the item's history chain."""

    @abstractmethod
    def clear(self, owner_id: str) -> int:
        """Erase every item, the digest and the mastery history."""

    @abstractmethod
    def touch(self, owner_id: str, ids: list[str]) -> None:
        """Mark items as used (eviction keeps what is useful). Not a version change."""

    @abstractmethod
    def enabled(self, owner_id: str) -> bool: ...

    @abstractmethod
    def set_enabled(self, owner_id: str, enabled: bool) -> None: ...

    # ── digest ───────────────────────────────────────────────────────────────
    @abstractmethod
    def save_profile(self, owner_id: str, content: str, built_version: int) -> None:
        """Store a digest — unless a newer one is already stored."""

    # ── upkeep ───────────────────────────────────────────────────────────────
    @abstractmethod
    def prune(self, *, retention_days: int, now: datetime | None = None) -> int:
        """Delete history (ended rows) and expired rows older than the retention window."""

    @abstractmethod
    def stale_embeddings(self, model: str, limit: int = 100) -> list[MemoryItem]:
        """Live items whose vector is missing or came from a different embedder."""

    @abstractmethod
    def set_embedding(self, owner_id: str, item_id: str, embedding: list[float], model: str) -> None: ...

    # ── mastery ──────────────────────────────────────────────────────────────
    @abstractmethod
    def mastery(self, owner_id: str, kc_refs: list[str] | None = None) -> list[MasteryRecord]: ...

    @abstractmethod
    def upsert_mastery(self, record: MasteryRecord) -> None: ...


def _eviction_victim(items, now: datetime) -> MemoryItem | None:
    """Least important, then least recently used. Explicit requests are never evicted."""
    pool = [i for i in items if not i.source.get("explicit")]
    if not pool:
        return None
    return min(pool, key=lambda i: (i.importance, i.last_used_at or i.created_at or now))


# ── in-memory (tests, single process) ────────────────────────────────────────
class _MemUnit(_Unit):
    def __init__(self, store: MemoryMemoryStore, owner_id: str) -> None:
        self.s, self.owner = store, owner_id

    def live(self):
        return self.s._live(self.owner)

    def insert(self, item):
        self.s._items[item.id] = copy.deepcopy(item)

    def end(self, item_id, reason, superseded_by=None):
        i = self.s._items.get(item_id)
        if i is not None and i.owner_id == self.owner:
            i.ended_at, i.end_reason = _now(), reason
            if superseded_by:
                i.superseded_by = superseded_by

    def bump(self, removal):
        self.s._bump(self.owner, removal)


class MemoryMemoryStore(MemoryStore):
    def __init__(self) -> None:
        self._items: dict[str, MemoryItem] = {}
        self._mastery: dict[tuple[str, str], MasteryRecord] = {}
        self._disabled: set[str] = set()
        self._versions: dict[str, list[int]] = {}       # owner → [version, removal_version]
        self._profiles: dict[str, StoredProfile] = {}
        self._lock = threading.RLock()

    @contextmanager
    def _unit(self, owner_id: str) -> Iterator[_Unit]:
        with self._lock:
            yield _MemUnit(self, owner_id)

    def _bump(self, owner_id: str, removal: bool) -> None:
        v = self._versions.setdefault(owner_id, [0, 0])
        v[0] += 1
        if removal:
            v[1] = v[0]

    def _live(self, owner_id: str) -> list[MemoryItem]:
        now = _now()
        rows = [copy.deepcopy(i) for i in self._items.values()
                if i.owner_id == owner_id and i.is_live(now)]
        return sorted(rows, key=lambda i: i.created_at, reverse=True)

    def live(self, owner_id):
        with self._lock:
            return self._live(owner_id)

    def state(self, owner_id):
        with self._lock:
            v = self._versions.get(owner_id, [0, 0])
            return MemoryState(enabled=owner_id not in self._disabled, version=v[0],
                               removal_version=v[1], profile=copy.copy(self._profiles.get(owner_id)))

    def delete(self, owner_id, item_id):
        with self._lock:
            i = self._items.get(item_id)
            if i is None or i.owner_id != owner_id or not i.is_live():
                return False
            # forget means forget: the item AND the history rows it replaced
            chain, frontier = {item_id}, {item_id}
            while frontier:
                frontier = {k for k, x in self._items.items()
                            if x.owner_id == owner_id and x.superseded_by in frontier} - chain
                chain |= frontier
            for k in chain:
                del self._items[k]
            self._bump(owner_id, removal=True)
            return True

    def clear(self, owner_id):
        with self._lock:
            ids = [k for k, i in self._items.items() if i.owner_id == owner_id]
            n = sum(1 for k in ids if self._items[k].is_live())
            for k in ids:
                del self._items[k]
            for k in [k for k in self._mastery if k[0] == owner_id]:
                del self._mastery[k]
            self._profiles.pop(owner_id, None)
            self._bump(owner_id, removal=True)
            return n

    def touch(self, owner_id, ids):
        now = _now()
        with self._lock:
            for k in ids:
                i = self._items.get(k)
                if i is not None and i.owner_id == owner_id:
                    i.last_used_at = now

    def enabled(self, owner_id):
        return owner_id not in self._disabled

    def set_enabled(self, owner_id, enabled):
        (self._disabled.discard if enabled else self._disabled.add)(owner_id)

    def save_profile(self, owner_id, content, built_version):
        with self._lock:
            cur = self._profiles.get(owner_id)
            if cur is None or cur.built_version <= built_version:
                self._profiles[owner_id] = StoredProfile(content, built_version, _now())

    def prune(self, *, retention_days, now=None):
        cutoff = (now or _now()) - timedelta(days=retention_days)
        with self._lock:
            dead = [k for k, i in self._items.items()
                    if (i.ended_at and i.ended_at < cutoff) or (i.expires_at and i.expires_at < cutoff)]
            for k in dead:
                del self._items[k]
            return len(dead)

    def stale_embeddings(self, model, limit=100):
        with self._lock:
            now = _now()
            return [copy.deepcopy(i) for i in self._items.values()
                    if i.is_live(now) and (i.embedding is None or i.embedding_model != model)][:limit]

    def set_embedding(self, owner_id, item_id, embedding, model):
        with self._lock:
            i = self._items.get(item_id)
            if i is not None and i.owner_id == owner_id:
                i.embedding, i.embedding_model = list(embedding), model
                self._bump(owner_id, removal=False)

    def mastery(self, owner_id, kc_refs=None):
        return [copy.deepcopy(r) for (o, k), r in self._mastery.items()
                if o == owner_id and (kc_refs is None or k in kc_refs)]

    def upsert_mastery(self, record):
        self._mastery[(record.owner_id, record.kc_ref)] = copy.deepcopy(record)


# ── Postgres ─────────────────────────────────────────────────────────────────
_ITEM = ("id, owner_id, kind, content, embedding, embedding_model, source, confidence, importance, "
         "valid_from, superseded_by, ended_at, end_reason, last_used_at, expires_at, created_at")
_MASTERY = "owner_id, kc_ref, title, p_mastery, attempts, correct, stability, last_seen, due_at"
_LIVE = "ended_at IS NULL AND superseded_by IS NULL AND (expires_at IS NULL OR expires_at > now())"
# namespace for the per-owner advisory lock (pg_advisory_xact_lock(int4, int4))
_LOCK_NS = 0x4C46_4D45 & 0x7FFF_FFFF     # "LFME"


def _item(r) -> MemoryItem:
    return MemoryItem(id=id_str(r["id"]), owner_id=id_str(r["owner_id"]), kind=r["kind"],
                      content=r["content"], embedding=r["embedding"], embedding_model=r["embedding_model"],
                      source=r["source"] or {}, confidence=r["confidence"], importance=r["importance"],
                      valid_from=r["valid_from"], superseded_by=id_str(r["superseded_by"]),
                      ended_at=r["ended_at"], end_reason=r["end_reason"], last_used_at=r["last_used_at"],
                      expires_at=r["expires_at"], created_at=r["created_at"])


def _mastery(r) -> MasteryRecord:
    return MasteryRecord(owner_id=id_str(r["owner_id"]), kc_ref=r["kc_ref"], title=r["title"],
                         p_mastery=r["p_mastery"], attempts=r["attempts"], correct=r["correct"],
                         stability=r["stability"], last_seen=r["last_seen"], due_at=r["due_at"])


_BUMP = ("INSERT INTO memory_settings (owner_id, enabled, version, removal_version, updated_at) "
         "VALUES (%(o)s, true, 1, CASE WHEN %(r)s THEN 1 ELSE 0 END, now()) "
         "ON CONFLICT (owner_id) DO UPDATE SET version = memory_settings.version + 1, "
         "removal_version = CASE WHEN %(r)s THEN memory_settings.version + 1 "
         "ELSE memory_settings.removal_version END, updated_at = now()")


class _PgUnit(_Unit):
    def __init__(self, cur, oid) -> None:
        self.cur, self.oid = cur, oid

    def live(self):
        self.cur.execute(f"SELECT {_ITEM} FROM user_memories WHERE owner_id = %s AND {_LIVE} "
                         "ORDER BY created_at DESC", (self.oid,))
        return [_item(r) for r in self.cur.fetchall()]

    def insert(self, item):
        PostgresMemoryStore._insert(self.cur, item)

    def end(self, item_id, reason, superseded_by=None):
        self.cur.execute("UPDATE user_memories SET ended_at = now(), end_reason = %s, "
                         "superseded_by = COALESCE(%s, superseded_by) WHERE id = %s AND owner_id = %s",
                         (reason, as_uuid(superseded_by), as_uuid(item_id), self.oid))

    def bump(self, removal):
        self.cur.execute(_BUMP, {"o": self.oid, "r": removal})


class _NullUnit(_Unit):
    """For an id that is not a UUID: there is nothing of theirs to touch."""

    def live(self):
        return []

    def insert(self, item):
        raise ValueError("invalid owner id")

    def end(self, item_id, reason, superseded_by=None):
        pass

    def bump(self, removal):
        pass


class PostgresMemoryStore(MemoryStore):
    def __init__(self, db: Database) -> None:
        self.db = db

    @contextmanager
    def _unit(self, owner_id: str) -> Iterator[_Unit]:
        oid = as_uuid(owner_id)
        if oid is None:
            yield _NullUnit()
            return
        with self.db.tx() as cur:
            key = int.from_bytes(oid.bytes[:4], "big", signed=True)
            cur.execute("SELECT pg_advisory_xact_lock(%s, %s)", (_LOCK_NS, key))
            yield _PgUnit(cur, oid)

    @staticmethod
    def _insert(cur, item: MemoryItem) -> None:
        cur.execute(
            f"INSERT INTO user_memories ({_ITEM}) VALUES ({', '.join(['%s'] * 16)})",
            (as_uuid(item.id), as_uuid(item.owner_id), item.kind, item.content,
             jsonb(item.embedding) if item.embedding is not None else None, item.embedding_model,
             jsonb(item.source), item.confidence, item.importance, item.valid_from,
             as_uuid(item.superseded_by), item.ended_at, item.end_reason, item.last_used_at,
             item.expires_at, item.created_at))

    def live(self, owner_id):
        oid = as_uuid(owner_id)
        if oid is None:
            return []
        with self.db.tx() as cur:
            return _PgUnit(cur, oid).live()

    def state(self, owner_id):
        oid = as_uuid(owner_id)
        if oid is None:
            return MemoryState(enabled=False)
        with self.db.tx() as cur:
            cur.execute(
                "SELECT s.enabled, s.version, s.removal_version, p.content, p.built_version, p.built_at "
                "FROM (SELECT %s::uuid AS owner_id) o "
                "LEFT JOIN memory_settings s ON s.owner_id = o.owner_id "
                "LEFT JOIN memory_profiles p ON p.owner_id = o.owner_id", (oid,))
            r = cur.fetchone()
        profile = (StoredProfile(r["content"], r["built_version"], r["built_at"])
                   if r and r["content"] is not None else None)
        return MemoryState(enabled=True if r is None or r["enabled"] is None else bool(r["enabled"]),
                           version=(r and r["version"]) or 0, removal_version=(r and r["removal_version"]) or 0,
                           profile=profile)

    def delete(self, owner_id, item_id):
        oid, iid = as_uuid(owner_id), as_uuid(item_id)
        if oid is None or iid is None:
            return False
        with self._unit(owner_id) as u:
            cur = u.cur
            cur.execute(f"SELECT 1 FROM user_memories WHERE id = %s AND owner_id = %s AND {_LIVE}", (iid, oid))
            if cur.fetchone() is None:
                return False
            # forget means forget: the item AND the history rows it replaced
            cur.execute(
                "WITH RECURSIVE chain(id) AS (SELECT %s::uuid UNION "
                "SELECT m.id FROM user_memories m JOIN chain c ON m.superseded_by = c.id "
                "WHERE m.owner_id = %s) "
                "DELETE FROM user_memories WHERE owner_id = %s AND id IN (SELECT id FROM chain)",
                (iid, oid, oid))
            u.bump(removal=True)
            return True

    def clear(self, owner_id):
        oid = as_uuid(owner_id)
        if oid is None:
            return 0
        with self._unit(owner_id) as u:
            cur = u.cur
            cur.execute(f"SELECT count(*) AS n FROM user_memories WHERE owner_id = %s AND {_LIVE}", (oid,))
            n = cur.fetchone()["n"]
            cur.execute("DELETE FROM user_memories WHERE owner_id = %s", (oid,))
            cur.execute("DELETE FROM mastery_records WHERE owner_id = %s", (oid,))
            cur.execute("DELETE FROM memory_profiles WHERE owner_id = %s", (oid,))
            u.bump(removal=True)
            return n

    def touch(self, owner_id, ids):
        uids = [u for u in (as_uuid(i) for i in ids) if u is not None]
        oid = as_uuid(owner_id)
        if not uids or oid is None:
            return
        with self.db.tx() as cur:
            cur.execute("UPDATE user_memories SET last_used_at = now() WHERE owner_id = %s AND id = ANY(%s)",
                        (oid, uids))

    def enabled(self, owner_id):
        return self.state(owner_id).enabled

    def set_enabled(self, owner_id, enabled):
        with self.db.tx() as cur:
            cur.execute("INSERT INTO memory_settings (owner_id, enabled, updated_at) VALUES (%s, %s, now()) "
                        "ON CONFLICT (owner_id) DO UPDATE SET enabled = EXCLUDED.enabled, updated_at = now()",
                        (as_uuid(owner_id), enabled))

    def save_profile(self, owner_id, content, built_version):
        oid = as_uuid(owner_id)
        if oid is None:
            return
        with self.db.tx() as cur:
            cur.execute("INSERT INTO memory_profiles (owner_id, content, built_version, built_at) "
                        "VALUES (%s, %s, %s, now()) ON CONFLICT (owner_id) DO UPDATE SET "
                        "content = EXCLUDED.content, built_version = EXCLUDED.built_version, built_at = now() "
                        "WHERE memory_profiles.built_version <= EXCLUDED.built_version",
                        (oid, content, built_version))

    def prune(self, *, retention_days, now=None):
        cutoff = (now or _now()) - timedelta(days=retention_days)
        with self.db.tx() as cur:
            cur.execute("DELETE FROM user_memories WHERE (ended_at IS NOT NULL AND ended_at < %s) "
                        "OR (expires_at IS NOT NULL AND expires_at < %s)", (cutoff, cutoff))
            return cur.rowcount

    def stale_embeddings(self, model, limit=100):
        with self.db.tx() as cur:
            cur.execute(f"SELECT {_ITEM} FROM user_memories WHERE {_LIVE} AND "
                        "(embedding IS NULL OR embedding_model IS DISTINCT FROM %s) LIMIT %s", (model, limit))
            return [_item(r) for r in cur.fetchall()]

    def set_embedding(self, owner_id, item_id, embedding, model):
        with self._unit(owner_id) as u:
            u.cur.execute("UPDATE user_memories SET embedding = %s, embedding_model = %s "
                          "WHERE id = %s AND owner_id = %s",
                          (jsonb([float(x) for x in embedding]), model, as_uuid(item_id), u.oid))
            u.bump(removal=False)

    def mastery(self, owner_id, kc_refs=None):
        oid = as_uuid(owner_id)
        if oid is None:
            return []
        with self.db.tx() as cur:
            if kc_refs is None:
                cur.execute(f"SELECT {_MASTERY} FROM mastery_records WHERE owner_id = %s ORDER BY due_at", (oid,))
            else:
                cur.execute(f"SELECT {_MASTERY} FROM mastery_records WHERE owner_id = %s AND kc_ref = ANY(%s)",
                            (oid, list(kc_refs)))
            return [_mastery(r) for r in cur.fetchall()]

    def upsert_mastery(self, record):
        r = record
        with self.db.tx() as cur:
            cur.execute(
                f"INSERT INTO mastery_records ({_MASTERY}) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (owner_id, kc_ref) DO UPDATE SET title = EXCLUDED.title, "
                "p_mastery = EXCLUDED.p_mastery, attempts = EXCLUDED.attempts, correct = EXCLUDED.correct, "
                "stability = EXCLUDED.stability, last_seen = EXCLUDED.last_seen, due_at = EXCLUDED.due_at",
                (as_uuid(r.owner_id), r.kc_ref, r.title, r.p_mastery, r.attempts, r.correct, r.stability,
                 r.last_seen, r.due_at))


STORE_REGISTRY: dict[str, Callable[[Database | None], MemoryStore]] = {
    "memory": lambda db: MemoryMemoryStore(),
    "postgres": lambda db: PostgresMemoryStore(_need(db)),
}


def _need(db: Database | None) -> Database:
    if db is None:
        raise ValueError("the postgres memory store needs database.url (DATABASE_URL)")
    return db


def build_memory_store(provider: str, *, db: Database | None = None) -> MemoryStore:
    try:
        return STORE_REGISTRY[provider](db)
    except KeyError:
        raise ValueError(f"unknown memory store {provider!r}; known: {sorted(STORE_REGISTRY)}") from None
