"""ChatStore — where conversations and their messages are persisted.

A storage port behind the same registry pattern as :class:`ProfileStore`: swap
``chat.store.provider`` in config and the backend changes with no code edit.

- ``memory`` — in-process dicts. Zero infra, non-persistent; the default seam for
  tests and a single-process dev run.
- ``sqlite`` — one file via the stdlib ``sqlite3`` driver. Persistent, no extra
  service, no new dependency.
- ``postgres`` — the real, Docker-backed store via ``psycopg``. DSN comes from
  ``chat.store.url`` (env-driven, e.g. ``${CHAT_DB_URL}``), never hard-coded.

The two SQL backends share all row logic in :class:`_SqlChatStore`; they differ
only in how they connect and in their placeholder token — the loose-coupling
contract applied to persistence.
"""

from __future__ import annotations

import json
import threading
from abc import ABC, abstractmethod
from contextlib import contextmanager
from datetime import UTC
from pathlib import Path
from typing import Any

from ...config import ChatStoreConfig
from ...domain.chat import ChatDefaults, ChatMessage, Conversation
from ..registry import register_chat_store


class ChatStore(ABC):
    """Persists conversations and messages, keyed by id. A missing conversation
    returns ``None`` (callers raise 404) rather than raising here."""

    @abstractmethod
    def create(self, conversation: Conversation) -> Conversation: ...

    @abstractmethod
    def get(self, conversation_id: str) -> Conversation | None: ...

    @abstractmethod
    def list(self, owner_id: str = "default") -> list[Conversation]:
        """Conversations for an owner, most-recently-updated first."""

    @abstractmethod
    def update(
        self,
        conversation_id: str,
        *,
        title: str | None = None,
        defaults: ChatDefaults | None = None,
        summary: str | None = None,
    ) -> Conversation | None:
        """Patch the given fields (each ``None`` = untouched) and bump
        ``updated_at``. Returns the updated conversation, or ``None`` if absent."""

    @abstractmethod
    def delete(self, conversation_id: str) -> bool:
        """Delete a conversation and its messages. Returns whether it existed."""

    @abstractmethod
    def add_message(self, conversation_id: str, message: ChatMessage) -> ChatMessage:
        """Append a message and touch the conversation's ``updated_at``."""

    @abstractmethod
    def messages(self, conversation_id: str, *, limit: int | None = None) -> list[ChatMessage]:
        """Messages in chronological order. ``limit`` keeps only the most recent
        ``limit`` (still returned oldest-first) for windowed memory."""

    @classmethod
    def from_config(cls, cfg: ChatStoreConfig) -> ChatStore:  # pragma: no cover
        raise NotImplementedError


@register_chat_store("memory")
class MemoryChatStore(ChatStore):
    """In-process store. Fast, dependency-free, non-persistent. Thread-safe so a
    threaded dev server doesn't corrupt the message lists mid-stream."""

    def __init__(self) -> None:
        self._convs: dict[str, Conversation] = {}
        self._msgs: dict[str, list[ChatMessage]] = {}
        self._lock = threading.Lock()

    @classmethod
    def from_config(cls, cfg: ChatStoreConfig) -> MemoryChatStore:
        return cls()

    def create(self, conversation: Conversation) -> Conversation:
        with self._lock:
            self._convs[conversation.id] = conversation
            self._msgs.setdefault(conversation.id, [])
        return conversation

    def get(self, conversation_id: str) -> Conversation | None:
        return self._convs.get(conversation_id)

    def list(self, owner_id: str = "default") -> list[Conversation]:
        convs = [c for c in self._convs.values() if c.owner_id == owner_id]
        return sorted(convs, key=lambda c: c.updated_at, reverse=True)

    def update(self, conversation_id, *, title=None, defaults=None, summary=None):
        with self._lock:
            conv = self._convs.get(conversation_id)
            if conv is None:
                return None
            data = conv.model_dump()
            if title is not None:
                data["title"] = title
            if defaults is not None:
                data["defaults"] = defaults.model_dump(by_alias=True)
            if summary is not None:
                data["summary"] = summary
            updated = Conversation.model_validate(data)
            updated = updated.model_copy(update={"updated_at": _now()})
            self._convs[conversation_id] = updated
            return updated

    def delete(self, conversation_id: str) -> bool:
        with self._lock:
            existed = self._convs.pop(conversation_id, None) is not None
            self._msgs.pop(conversation_id, None)
        return existed

    def add_message(self, conversation_id: str, message: ChatMessage) -> ChatMessage:
        with self._lock:
            self._msgs.setdefault(conversation_id, []).append(message)
            conv = self._convs.get(conversation_id)
            if conv is not None:
                self._convs[conversation_id] = conv.model_copy(update={"updated_at": _now()})
        return message

    def messages(self, conversation_id, *, limit=None) -> list[ChatMessage]:
        msgs = self._msgs.get(conversation_id, [])
        return list(msgs[-limit:]) if limit else list(msgs)


def _now():
    from datetime import datetime

    return datetime.now(UTC)


# ── SQL backends (sqlite + postgres share everything but connection + dialect) ──
class _SqlChatStore(ChatStore):
    """Shared SQL implementation. Stores queryable columns natively and the
    nested/free-form parts (defaults, citations, meta) as JSON text — dialect-
    agnostic, so the same row logic serves both sqlite and postgres. Subclasses
    supply :meth:`_connect`, the placeholder token ``_ph``, and the id column DDL."""

    _ph = "?"          # parameter placeholder (sqlite "?", postgres "%s")

    @contextmanager
    def _connect(self):  # pragma: no cover - overridden
        raise NotImplementedError
        yield

    def _q(self, sql: str) -> str:
        """Translate the placeholder token so subclasses write one SQL string."""
        return sql.replace("?", self._ph) if self._ph != "?" else sql

    def _init_schema(self) -> None:
        with self._connect() as (conn, cur):
            cur.execute(
                "CREATE TABLE IF NOT EXISTS chat_conversations ("
                " id TEXT PRIMARY KEY,"
                " owner_id TEXT NOT NULL,"
                " title TEXT NOT NULL,"
                " defaults TEXT NOT NULL,"
                " summary TEXT NOT NULL,"
                " created_at TEXT NOT NULL,"
                " updated_at TEXT NOT NULL)"
            )
            cur.execute(
                "CREATE TABLE IF NOT EXISTS chat_messages ("
                " id TEXT PRIMARY KEY,"
                " conversation_id TEXT NOT NULL,"
                " ord INTEGER NOT NULL,"
                " role TEXT NOT NULL,"
                " content TEXT NOT NULL,"
                " citations TEXT NOT NULL,"
                " created_at TEXT NOT NULL,"
                " meta TEXT NOT NULL)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS ix_chat_messages_conv "
                "ON chat_messages (conversation_id, ord)"
            )
            conn.commit()

    # ── (de)serialization ────────────────────────────────────────────────────
    @staticmethod
    def _conv_row(c: Conversation) -> tuple:
        return (
            c.id, c.owner_id, c.title,
            c.defaults.model_dump_json(by_alias=True), c.summary,
            c.created_at.isoformat(), c.updated_at.isoformat(),
        )

    @staticmethod
    def _conv_from_row(r: Any) -> Conversation:
        return Conversation(
            id=r[0], owner_id=r[1], title=r[2],
            defaults=ChatDefaults.model_validate_json(r[3]), summary=r[4],
            created_at=r[5], updated_at=r[6],
        )

    @staticmethod
    def _msg_from_row(r: Any) -> ChatMessage:
        return ChatMessage(
            id=r[0], role=r[1], content=r[2],
            citations=json.loads(r[3]), created_at=r[4], meta=json.loads(r[5]),
        )

    # ── CRUD ─────────────────────────────────────────────────────────────────
    def create(self, conversation: Conversation) -> Conversation:
        with self._connect() as (conn, cur):
            cur.execute(
                self._q(
                    "INSERT INTO chat_conversations "
                    "(id, owner_id, title, defaults, summary, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)"
                ),
                self._conv_row(conversation),
            )
            conn.commit()
        return conversation

    def get(self, conversation_id: str) -> Conversation | None:
        with self._connect() as (_conn, cur):
            cur.execute(
                self._q(
                    "SELECT id, owner_id, title, defaults, summary, created_at, updated_at "
                    "FROM chat_conversations WHERE id = ?"
                ),
                (conversation_id,),
            )
            row = cur.fetchone()
        return self._conv_from_row(row) if row else None

    def list(self, owner_id: str = "default") -> list[Conversation]:
        with self._connect() as (_conn, cur):
            cur.execute(
                self._q(
                    "SELECT id, owner_id, title, defaults, summary, created_at, updated_at "
                    "FROM chat_conversations WHERE owner_id = ? ORDER BY updated_at DESC"
                ),
                (owner_id,),
            )
            rows = cur.fetchall()
        return [self._conv_from_row(r) for r in rows]

    def update(self, conversation_id, *, title=None, defaults=None, summary=None):
        conv = self.get(conversation_id)
        if conv is None:
            return None
        updated = conv.model_copy(update={
            "title": conv.title if title is None else title,
            "defaults": conv.defaults if defaults is None else defaults,
            "summary": conv.summary if summary is None else summary,
            "updated_at": _now(),
        })
        with self._connect() as (conn, cur):
            cur.execute(
                self._q(
                    "UPDATE chat_conversations SET title=?, defaults=?, summary=?, updated_at=? "
                    "WHERE id=?"
                ),
                (
                    updated.title, updated.defaults.model_dump_json(by_alias=True),
                    updated.summary, updated.updated_at.isoformat(), conversation_id,
                ),
            )
            conn.commit()
        return updated

    def delete(self, conversation_id: str) -> bool:
        with self._connect() as (conn, cur):
            cur.execute(
                self._q("SELECT 1 FROM chat_conversations WHERE id = ?"), (conversation_id,)
            )
            existed = cur.fetchone() is not None
            cur.execute(
                self._q("DELETE FROM chat_messages WHERE conversation_id = ?"), (conversation_id,)
            )
            cur.execute(
                self._q("DELETE FROM chat_conversations WHERE id = ?"), (conversation_id,)
            )
            conn.commit()
        return existed

    def add_message(self, conversation_id: str, message: ChatMessage) -> ChatMessage:
        with self._connect() as (conn, cur):
            cur.execute(
                self._q("SELECT COALESCE(MAX(ord), -1) + 1 FROM chat_messages "
                        "WHERE conversation_id = ?"),
                (conversation_id,),
            )
            ord_ = int(cur.fetchone()[0])
            cur.execute(
                self._q(
                    "INSERT INTO chat_messages "
                    "(id, conversation_id, ord, role, content, citations, created_at, meta) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                ),
                (
                    message.id, conversation_id, ord_, message.role.value, message.content,
                    json.dumps([c.model_dump() for c in message.citations]),
                    message.created_at.isoformat(), json.dumps(message.meta),
                ),
            )
            cur.execute(
                self._q("UPDATE chat_conversations SET updated_at=? WHERE id=?"),
                (_now().isoformat(), conversation_id),
            )
            conn.commit()
        return message

    def messages(self, conversation_id, *, limit=None) -> list[ChatMessage]:
        with self._connect() as (_conn, cur):
            if limit:
                cur.execute(
                    self._q(
                        "SELECT id, role, content, citations, created_at, meta "
                        "FROM chat_messages WHERE conversation_id = ? "
                        "ORDER BY ord DESC LIMIT ?"
                    ),
                    (conversation_id, limit),
                )
                rows = list(reversed(cur.fetchall()))
            else:
                cur.execute(
                    self._q(
                        "SELECT id, role, content, citations, created_at, meta "
                        "FROM chat_messages WHERE conversation_id = ? ORDER BY ord ASC"
                    ),
                    (conversation_id,),
                )
                rows = cur.fetchall()
        return [self._msg_from_row(r) for r in rows]


@register_chat_store("sqlite")
class SqliteChatStore(_SqlChatStore):
    """One SQLite file. Persistent, no service, no new dependency. Serialized
    access (one connection guarded by a lock) keeps a threaded dev server safe."""

    def __init__(self, path: str | Path) -> None:
        import sqlite3

        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False + a lock: fine for the app's modest write rate,
        # and it keeps the store a single simple object.
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._lock = threading.Lock()
        self._init_schema()

    @classmethod
    def from_config(cls, cfg: ChatStoreConfig) -> SqliteChatStore:
        return cls(cfg.path or ".lessonforge/chat.db")

    @contextmanager
    def _connect(self):
        with self._lock:
            yield self._conn, self._conn.cursor()


@register_chat_store("postgres")
class PostgresChatStore(_SqlChatStore):
    """The Docker-backed store via ``psycopg`` (v3). DSN from config ``url`` (an
    env-interpolated ``${CHAT_DB_URL}``). A short-lived connection per operation
    keeps it robust to the DB restarting under it."""

    _ph = "%s"

    def __init__(self, dsn: str) -> None:
        if not dsn:
            raise ValueError(
                "postgres chat store needs a DSN — set chat.store.url (e.g. ${CHAT_DB_URL})."
            )
        self.dsn = dsn
        self._init_schema()

    @classmethod
    def from_config(cls, cfg: ChatStoreConfig) -> PostgresChatStore:
        return cls(cfg.url or "")

    @contextmanager
    def _connect(self):
        import psycopg

        conn = psycopg.connect(self.dsn)
        try:
            yield conn, conn.cursor()
        finally:
            conn.close()
