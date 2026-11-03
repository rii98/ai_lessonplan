"""LearningSessionStore — persistence for learn-mode sessions (and their turn log).

The session is one structured document (:class:`LearningSession`) saved with
**optimistic concurrency**: ``save`` succeeds only if the stored ``version`` is the
one the caller loaded, so two tabs answering at once can't silently overwrite each
other (the loser gets :class:`SessionConflict` and the UI refreshes).

``memory`` (tests, single process) and ``postgres`` (the shared pool) — same port,
same contract tests.
"""

from __future__ import annotations

import copy
import logging
import threading
from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from ...db.pool import Database, as_uuid, jsonb
from ...domain.learning import LearningSession

CURRENT = ("planning", "active", "paused")
log = logging.getLogger("lessonforge.learn.store")


class SessionConflict(RuntimeError):
    """The session changed since it was loaded (another tab/turn saved first)."""


class LearningSessionStore(ABC):
    @abstractmethod
    def create(self, session: LearningSession) -> LearningSession: ...

    @abstractmethod
    def get(self, session_id: str, *, owner_id: str) -> LearningSession | None: ...

    @abstractmethod
    def current(self, conversation_id: str) -> LearningSession | None:
        """The conversation's most recent active-or-paused session, if any."""

    @abstractmethod
    def list_for_conversation(self, conversation_id: str) -> list[LearningSession]: ...

    @abstractmethod
    def save(self, session: LearningSession) -> LearningSession:
        """Persist; bumps ``version``. Raises :class:`SessionConflict` on a stale write."""

    @abstractmethod
    def log(self, session_id: str, event: dict[str, Any]) -> None:
        """Append one turn record (move, trace, grading…) — best effort."""


def _now() -> datetime:
    return datetime.now(UTC)


class MemoryLearningSessionStore(LearningSessionStore):
    def __init__(self) -> None:
        self._rows: dict[str, LearningSession] = {}
        self._events: dict[str, list[dict[str, Any]]] = {}
        self._lock = threading.Lock()

    def create(self, session):
        with self._lock:
            if session.status in ("planning", "active") and any(
                    s.conversation_id == session.conversation_id and s.status in ("planning", "active")
                    for s in self._rows.values()):
                raise SessionConflict("the conversation already has an active session")
            self._rows[session.id] = copy.deepcopy(session)
        return session

    def get(self, session_id, *, owner_id):
        s = self._rows.get(session_id)
        return copy.deepcopy(s) if s and s.owner_id == owner_id else None

    def current(self, conversation_id):
        rows = [s for s in self._rows.values()
                if s.conversation_id == conversation_id and s.status in CURRENT]
        if not rows:
            return None
        rows.sort(key=lambda s: (s.status in ("planning", "active"), s.updated_at), reverse=True)
        return copy.deepcopy(rows[0])

    def list_for_conversation(self, conversation_id):
        return [copy.deepcopy(s) for s in sorted(self._rows.values(), key=lambda s: s.created_at)
                if s.conversation_id == conversation_id]

    def save(self, session):
        with self._lock:
            stored = self._rows.get(session.id)
            if stored is None or stored.version != session.version:
                raise SessionConflict(session.id)
            if session.status in ("planning", "active") and any(
                    s.id != session.id and s.conversation_id == session.conversation_id
                    and s.status in ("planning", "active") for s in self._rows.values()):
                raise SessionConflict("the conversation already has an active session")
            session.version += 1
            session.updated_at = _now()
            self._rows[session.id] = copy.deepcopy(session)
        return session

    def log(self, session_id, event):
        self._events.setdefault(session_id, []).append(dict(event))

    def events(self, session_id: str) -> list[dict[str, Any]]:   # test helper
        return list(self._events.get(session_id, []))


class PostgresLearningSessionStore(LearningSessionStore):
    def __init__(self, db: Database) -> None:
        self.db = db

    @staticmethod
    def _load(row: dict[str, Any]) -> LearningSession:
        s = LearningSession.model_validate(row["data"])
        s.version = row["version"]
        return s

    def create(self, session):
        try:
            with self.db.tx() as cur:
                cur.execute(
                    "INSERT INTO learning_sessions (id, conversation_id, owner_id, status, topic, data, "
                    "version, created_at, updated_at) VALUES (%s, %s, %s, %s, %s, %s, 0, %s, %s)",
                    (as_uuid(session.id), as_uuid(session.conversation_id), as_uuid(session.owner_id),
                     session.status, session.plan.topic, jsonb(session.model_dump(mode="json")),
                     session.created_at, session.updated_at))
        except Exception as exc:
            if "uq_learning_sessions_active" in str(exc):
                raise SessionConflict("the conversation already has an active session") from exc
            raise
        session.version = 0
        return session

    def get(self, session_id, *, owner_id):
        sid, oid = as_uuid(session_id), as_uuid(owner_id)
        if sid is None or oid is None:
            return None
        with self.db.tx() as cur:
            cur.execute("SELECT data, version FROM learning_sessions WHERE id = %s AND owner_id = %s",
                        (sid, oid))
            row = cur.fetchone()
        return self._load(row) if row else None

    def current(self, conversation_id):
        cid = as_uuid(conversation_id)
        if cid is None:
            return None
        with self.db.tx() as cur:
            cur.execute("SELECT data, version FROM learning_sessions WHERE conversation_id = %s "
                        "AND status IN ('planning','active','paused') "
                        "ORDER BY (status IN ('planning','active')) DESC, updated_at DESC LIMIT 1",
                        (cid,))
            row = cur.fetchone()
        return self._load(row) if row else None

    def list_for_conversation(self, conversation_id):
        with self.db.tx() as cur:
            cur.execute("SELECT data, version FROM learning_sessions WHERE conversation_id = %s "
                        "ORDER BY created_at", (as_uuid(conversation_id),))
            return [self._load(r) for r in cur.fetchall()]

    def save(self, session):
        expected = session.version
        session.version = expected + 1
        session.updated_at = _now()
        try:
            with self.db.tx() as cur:
                cur.execute(
                    "UPDATE learning_sessions SET status = %s, data = %s, version = %s, updated_at = %s "
                    "WHERE id = %s AND version = %s RETURNING 1",
                    (session.status, jsonb(session.model_dump(mode="json")), session.version,
                     session.updated_at, as_uuid(session.id), expected))
                ok = cur.fetchone() is not None
        except Exception as exc:
            session.version = expected
            if "uq_learning_sessions_active" in str(exc):
                raise SessionConflict("the conversation already has an active session") from exc
            raise
        if not ok:
            session.version = expected
            raise SessionConflict(session.id)
        return session

    def log(self, session_id, event):
        try:
            with self.db.tx() as cur:
                cur.execute("INSERT INTO learning_events (session_id, data) VALUES (%s, %s)",
                            (as_uuid(session_id), jsonb(event)))
        except Exception:   # the turn log is diagnostics — never fail a turn over it
            log.warning("learning event log failed", exc_info=True)


STORE_REGISTRY: dict[str, Callable[[Database | None], LearningSessionStore]] = {
    "memory": lambda db: MemoryLearningSessionStore(),
    "postgres": lambda db: PostgresLearningSessionStore(_need(db)),
}


def _need(db: Database | None) -> Database:
    if db is None:
        raise ValueError("the postgres learning-session store needs database.url (DATABASE_URL)")
    return db


def build_session_store(provider: str, *, db: Database | None = None) -> LearningSessionStore:
    try:
        return STORE_REGISTRY[provider](db)
    except KeyError:
        raise ValueError(f"unknown learning-session store {provider!r}; "
                         f"known: {sorted(STORE_REGISTRY)}") from None
