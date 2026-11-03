"""MemoryStore — owner-isolated persistence for memory items, mastery and the per-user
memory switch. Every read and write is keyed by ``owner_id``; there is no cross-user
query. ``memory`` (tests) and ``postgres`` (shared pool) implement the same contract.

Similarity search is per user over a small set (hundreds of items at most), so it is a
brute-force cosine in Python over stored embeddings — no vector extension, no new
infrastructure. The port allows a pgvector/Qdrant implementation later.
"""

from __future__ import annotations

import copy
import threading
from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import UTC, datetime

from ...db.pool import Database, as_uuid, id_str, jsonb
from ...domain.memory import MasteryRecord, MemoryItem


def _now() -> datetime:
    return datetime.now(UTC)


class MemoryStore(ABC):
    # ── items ────────────────────────────────────────────────────────────────
    @abstractmethod
    def add(self, item: MemoryItem) -> MemoryItem: ...

    @abstractmethod
    def live(self, owner_id: str) -> list[MemoryItem]:
        """Current (not superseded, not expired) items, newest first."""

    @abstractmethod
    def supersede(self, owner_id: str, old_id: str, new: MemoryItem) -> MemoryItem | None:
        """Replace an item, keeping its history. ``None`` if ``old_id`` isn't the owner's."""

    @abstractmethod
    def delete(self, owner_id: str, item_id: str) -> bool: ...

    @abstractmethod
    def clear(self, owner_id: str) -> int: ...

    @abstractmethod
    def touch(self, owner_id: str, ids: list[str]) -> None:
        """Mark items as used (retention keeps what is useful)."""

    # ── settings ─────────────────────────────────────────────────────────────
    @abstractmethod
    def enabled(self, owner_id: str) -> bool: ...

    @abstractmethod
    def set_enabled(self, owner_id: str, enabled: bool) -> None: ...

    # ── mastery ──────────────────────────────────────────────────────────────
    @abstractmethod
    def mastery(self, owner_id: str, kc_refs: list[str] | None = None) -> list[MasteryRecord]: ...

    @abstractmethod
    def upsert_mastery(self, record: MasteryRecord) -> None: ...


class MemoryMemoryStore(MemoryStore):
    def __init__(self) -> None:
        self._items: dict[str, MemoryItem] = {}
        self._mastery: dict[tuple[str, str], MasteryRecord] = {}
        self._disabled: set[str] = set()
        self._lock = threading.Lock()

    def add(self, item):
        with self._lock:
            self._items[item.id] = copy.deepcopy(item)
        return item

    def live(self, owner_id):
        now = _now()
        rows = [copy.deepcopy(i) for i in self._items.values()
                if i.owner_id == owner_id and i.superseded_by is None
                and (i.expires_at is None or i.expires_at > now)]
        return sorted(rows, key=lambda i: i.created_at, reverse=True)

    def supersede(self, owner_id, old_id, new):
        with self._lock:
            old = self._items.get(old_id)
            if old is None or old.owner_id != owner_id:
                return None
            self._items[new.id] = copy.deepcopy(new)
            old.superseded_by = new.id
        return new

    def delete(self, owner_id, item_id):
        with self._lock:
            i = self._items.get(item_id)
            if i is None or i.owner_id != owner_id:
                return False
            del self._items[item_id]
            return True

    def clear(self, owner_id):
        with self._lock:
            ids = [k for k, i in self._items.items() if i.owner_id == owner_id]
            for k in ids:
                del self._items[k]
            for k in [k for k in self._mastery if k[0] == owner_id]:
                del self._mastery[k]
            return len(ids)

    def touch(self, owner_id, ids):
        now = _now()
        for k in ids:
            i = self._items.get(k)
            if i is not None and i.owner_id == owner_id:
                i.last_used_at = now

    def enabled(self, owner_id):
        return owner_id not in self._disabled

    def set_enabled(self, owner_id, enabled):
        (self._disabled.discard if enabled else self._disabled.add)(owner_id)

    def mastery(self, owner_id, kc_refs=None):
        return [copy.deepcopy(r) for (o, k), r in self._mastery.items()
                if o == owner_id and (kc_refs is None or k in kc_refs)]

    def upsert_mastery(self, record):
        self._mastery[(record.owner_id, record.kc_ref)] = copy.deepcopy(record)


_ITEM = ("id, owner_id, kind, content, embedding, source, confidence, valid_from, superseded_by, "
         "last_used_at, expires_at, created_at")
_MASTERY = "owner_id, kc_ref, title, p_mastery, attempts, correct, stability, last_seen, due_at"


def _item(r) -> MemoryItem:
    return MemoryItem(id=id_str(r["id"]), owner_id=id_str(r["owner_id"]), kind=r["kind"],
                      content=r["content"], embedding=r["embedding"], source=r["source"] or {},
                      confidence=r["confidence"], valid_from=r["valid_from"],
                      superseded_by=id_str(r["superseded_by"]), last_used_at=r["last_used_at"],
                      expires_at=r["expires_at"], created_at=r["created_at"])


def _mastery(r) -> MasteryRecord:
    return MasteryRecord(owner_id=id_str(r["owner_id"]), kc_ref=r["kc_ref"], title=r["title"],
                         p_mastery=r["p_mastery"], attempts=r["attempts"], correct=r["correct"],
                         stability=r["stability"], last_seen=r["last_seen"], due_at=r["due_at"])


class PostgresMemoryStore(MemoryStore):
    def __init__(self, db: Database) -> None:
        self.db = db

    def add(self, item):
        with self.db.tx() as cur:
            self._insert(cur, item)
        return item

    @staticmethod
    def _insert(cur, item: MemoryItem) -> None:
        cur.execute(
            f"INSERT INTO user_memories ({_ITEM}) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (as_uuid(item.id), as_uuid(item.owner_id), item.kind, item.content,
             jsonb(item.embedding) if item.embedding is not None else None, jsonb(item.source),
             item.confidence, item.valid_from, as_uuid(item.superseded_by), item.last_used_at,
             item.expires_at, item.created_at))

    def live(self, owner_id):
        oid = as_uuid(owner_id)
        if oid is None:
            return []
        with self.db.tx() as cur:
            cur.execute(f"SELECT {_ITEM} FROM user_memories WHERE owner_id = %s AND superseded_by IS NULL "
                        "AND (expires_at IS NULL OR expires_at > now()) ORDER BY created_at DESC", (oid,))
            return [_item(r) for r in cur.fetchall()]

    def supersede(self, owner_id, old_id, new):
        oid, old = as_uuid(owner_id), as_uuid(old_id)
        if oid is None or old is None:
            return None
        with self.db.tx() as cur:
            cur.execute("SELECT 1 FROM user_memories WHERE id = %s AND owner_id = %s FOR UPDATE", (old, oid))
            if cur.fetchone() is None:
                return None
            self._insert(cur, new)
            cur.execute("UPDATE user_memories SET superseded_by = %s WHERE id = %s", (as_uuid(new.id), old))
        return new

    def delete(self, owner_id, item_id):
        oid, iid = as_uuid(owner_id), as_uuid(item_id)
        if oid is None or iid is None:
            return False
        with self.db.tx() as cur:
            cur.execute("DELETE FROM user_memories WHERE id = %s AND owner_id = %s RETURNING 1", (iid, oid))
            return cur.fetchone() is not None

    def clear(self, owner_id):
        oid = as_uuid(owner_id)
        if oid is None:
            return 0
        with self.db.tx() as cur:
            cur.execute("DELETE FROM user_memories WHERE owner_id = %s", (oid,))
            n = cur.rowcount
            cur.execute("DELETE FROM mastery_records WHERE owner_id = %s", (oid,))
            return n

    def touch(self, owner_id, ids):
        uids = [u for u in (as_uuid(i) for i in ids) if u is not None]
        if not uids:
            return
        with self.db.tx() as cur:
            cur.execute("UPDATE user_memories SET last_used_at = now() WHERE owner_id = %s AND id = ANY(%s)",
                        (as_uuid(owner_id), uids))

    def enabled(self, owner_id):
        oid = as_uuid(owner_id)
        if oid is None:
            return False
        with self.db.tx() as cur:
            cur.execute("SELECT enabled FROM memory_settings WHERE owner_id = %s", (oid,))
            row = cur.fetchone()
        return True if row is None else bool(row["enabled"])

    def set_enabled(self, owner_id, enabled):
        with self.db.tx() as cur:
            cur.execute("INSERT INTO memory_settings (owner_id, enabled, updated_at) VALUES (%s, %s, now()) "
                        "ON CONFLICT (owner_id) DO UPDATE SET enabled = EXCLUDED.enabled, updated_at = now()",
                        (as_uuid(owner_id), enabled))

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
