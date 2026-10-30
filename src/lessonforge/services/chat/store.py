"""ChatStore — where conversations, messages, artifacts and attempts are persisted.

A storage port behind the registry pattern: ``chat.store.provider`` picks
``memory`` (tests) or ``postgres`` (the shared pool). Both implement identical
semantics; one contract test suite runs against each.

Access rules live at the entry points (D7): every conversation, artifact and
attempt that can be named from outside is looked up together with its owner
(``owner_id`` = the caller's principal id), and a foreign row is simply "not
found". Children of an already-authorized conversation (its messages, its
artifact list) are read by conversation id.

Efficiency notes:

- a message's ``ord`` is allocated by one atomic ``UPDATE … RETURNING`` on the
  conversation's counter (no ``max()+1`` race, no extra read);
- a chat turn reads only the transcript *tail* it needs, never the whole history;
- artifact summaries load every attempt of a conversation in ONE query
  (:meth:`attempts_by_artifact`), not one query per artifact.
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from ...config import ChatStoreConfig
from ...db.pool import Database, as_uuid, id_str, jsonb
from ...domain.chat import (
    ArtifactAttempt,
    ChatArtifact,
    ChatDefaults,
    ChatMessage,
    Conversation,
)
from ...pagination import Keyset, Page, page_of
from ..registry import register_chat_store


def _now() -> datetime:
    return datetime.now(UTC)


class ChatStore(ABC):
    # ── conversations (owner-scoped) ─────────────────────────────────────────
    @abstractmethod
    def create(self, conversation: Conversation) -> Conversation: ...

    @abstractmethod
    def get(self, conversation_id: str, *, owner_id: str) -> Conversation | None: ...

    @abstractmethod
    def list(self, owner_id: str, *, limit: int, cursor: Keyset | None = None) -> Page[Conversation]:
        """An owner's conversations, most-recently-updated first, keyset-paged."""

    @abstractmethod
    def update(self, conversation_id: str, *, owner_id: str, title: str | None = None,
               defaults: ChatDefaults | None = None, summary: str | None = None
               ) -> Conversation | None:
        """Patch the given fields (``None`` = untouched) and bump ``updated_at``."""

    @abstractmethod
    def delete(self, conversation_id: str, *, owner_id: str) -> bool:
        """Delete a conversation with its messages, artifacts and attempts."""

    # ── messages (conversation already authorized) ───────────────────────────
    @abstractmethod
    def add_message(self, conversation_id: str, message: ChatMessage) -> ChatMessage:
        """Append a message (next ``ord``) and touch the conversation."""

    @abstractmethod
    def messages(self, conversation_id: str) -> list[ChatMessage]:
        """The whole transcript, oldest first (reloading a thread)."""

    @abstractmethod
    def tail(self, conversation_id: str, *, limit: int) -> tuple[list[ChatMessage], int]:
        """The last ``limit`` messages (oldest first) and the transcript length."""

    @abstractmethod
    def messages_between(self, conversation_id: str, start: int, end: int) -> list[ChatMessage]:
        """Messages at positions ``[start, end)`` (for rolling summaries)."""

    # ── artifacts ────────────────────────────────────────────────────────────
    @abstractmethod
    def add_artifact(self, artifact: ChatArtifact) -> ChatArtifact: ...

    @abstractmethod
    def get_artifact(self, artifact_id: str, *, owner_id: str) -> ChatArtifact | None: ...

    @abstractmethod
    def list_artifacts(self, conversation_id: str) -> list[ChatArtifact]:
        """A conversation's artifacts, oldest first."""

    @abstractmethod
    def delete_artifact(self, artifact_id: str, *, owner_id: str) -> bool: ...

    # ── attempts ─────────────────────────────────────────────────────────────
    @abstractmethod
    def save_attempt(self, attempt: ArtifactAttempt) -> ArtifactAttempt:
        """Insert or update an attempt (it is rewritten as the user answers)."""

    @abstractmethod
    def get_attempt(self, attempt_id: str, *, owner_id: str) -> ArtifactAttempt | None: ...

    @abstractmethod
    def list_attempts(self, artifact_id: str) -> list[ArtifactAttempt]:
        """An artifact's attempts, newest first."""

    @abstractmethod
    def counts(self) -> dict[str, int]:
        """Totals for the owner console: conversations, messages, quizzes, attempts."""

    @abstractmethod
    def attempts_by_artifact(self, conversation_id: str) -> dict[str, list[ArtifactAttempt]]:
        """Every attempt in a conversation grouped by artifact (newest first) — one
        read for all of a conversation's artifact summaries."""

    @classmethod
    def from_config(cls, cfg: ChatStoreConfig, *, db: Database | None = None) -> ChatStore:  # pragma: no cover
        raise NotImplementedError


def _conv_key(c: Conversation) -> Keyset:
    return Keyset(c.updated_at, c.id)


# ── memory ───────────────────────────────────────────────────────────────────
@register_chat_store("memory")
class MemoryChatStore(ChatStore):
    """In-process store. Dependency-free, non-persistent, thread-safe."""

    def __init__(self) -> None:
        self._convs: dict[str, Conversation] = {}
        self._msgs: dict[str, list[ChatMessage]] = {}
        self._artifacts: dict[str, ChatArtifact] = {}
        self._attempts: dict[str, ArtifactAttempt] = {}
        self._lock = threading.RLock()

    @classmethod
    def from_config(cls, cfg: ChatStoreConfig, *, db: Database | None = None) -> MemoryChatStore:
        return cls()

    def _owned(self, conversation_id: str, owner_id: str) -> Conversation | None:
        c = self._convs.get(conversation_id)
        return c if c is not None and c.owner_id == owner_id else None

    def create(self, conversation):
        with self._lock:
            self._convs[conversation.id] = conversation
            self._msgs.setdefault(conversation.id, [])
        return conversation

    def get(self, conversation_id, *, owner_id):
        return self._owned(conversation_id, owner_id)

    def list(self, owner_id, *, limit, cursor=None):
        convs = sorted((c for c in self._convs.values() if c.owner_id == owner_id),
                       key=lambda c: (c.updated_at, c.id), reverse=True)
        if cursor is not None:
            convs = [c for c in convs if cursor.before(c.updated_at, c.id)]
        return page_of(convs[:limit + 1], limit, _conv_key)

    def update(self, conversation_id, *, owner_id, title=None, defaults=None, summary=None):
        with self._lock:
            conv = self._owned(conversation_id, owner_id)
            if conv is None:
                return None
            patch: dict[str, Any] = {"updated_at": _now()}
            if title is not None:
                patch["title"] = title
            if defaults is not None:
                patch["defaults"] = defaults
            if summary is not None:
                patch["summary"] = summary
            updated = conv.model_copy(update=patch)
            self._convs[conversation_id] = updated
            return updated

    def delete(self, conversation_id, *, owner_id):
        with self._lock:
            if self._owned(conversation_id, owner_id) is None:
                return False
            del self._convs[conversation_id]
            self._msgs.pop(conversation_id, None)
            gone = {a.id for a in self._artifacts.values() if a.conversation_id == conversation_id}
            for aid in gone:
                del self._artifacts[aid]
            for tid in [t.id for t in self._attempts.values() if t.conversation_id == conversation_id]:
                del self._attempts[tid]
            return True

    def add_message(self, conversation_id, message):
        with self._lock:
            msgs = self._msgs.setdefault(conversation_id, [])
            msgs.append(message)
            conv = self._convs.get(conversation_id)
            if conv is not None:
                self._convs[conversation_id] = conv.model_copy(
                    update={"updated_at": _now(), "message_count": len(msgs)})
        return message

    def messages(self, conversation_id):
        return list(self._msgs.get(conversation_id, []))

    def tail(self, conversation_id, *, limit):
        msgs = self._msgs.get(conversation_id, [])
        return list(msgs[-limit:]) if limit > 0 else [], len(msgs)

    def messages_between(self, conversation_id, start, end):
        return list(self._msgs.get(conversation_id, [])[max(0, start):max(0, end)])

    def add_artifact(self, artifact):
        with self._lock:
            self._artifacts[artifact.id] = artifact
        return artifact

    def get_artifact(self, artifact_id, *, owner_id):
        art = self._artifacts.get(artifact_id)
        if art is None or self._owned(art.conversation_id, owner_id) is None:
            return None
        return art

    def list_artifacts(self, conversation_id):
        return sorted((a for a in self._artifacts.values() if a.conversation_id == conversation_id),
                      key=lambda a: a.created_at)

    def delete_artifact(self, artifact_id, *, owner_id):
        with self._lock:
            if self.get_artifact(artifact_id, owner_id=owner_id) is None:
                return False
            del self._artifacts[artifact_id]
            for tid in [t.id for t in self._attempts.values() if t.artifact_id == artifact_id]:
                del self._attempts[tid]
            return True

    def save_attempt(self, attempt):
        with self._lock:
            self._attempts[attempt.id] = attempt
        return attempt

    def get_attempt(self, attempt_id, *, owner_id):
        att = self._attempts.get(attempt_id)
        return att if att is not None and att.owner_id == owner_id else None

    def list_attempts(self, artifact_id):
        return sorted((t for t in self._attempts.values() if t.artifact_id == artifact_id),
                      key=lambda t: t.created_at, reverse=True)

    def counts(self):
        return {"conversations": len(self._convs),
                "messages": sum(len(m) for m in self._msgs.values()),
                "artifacts": len(self._artifacts), "attempts": len(self._attempts)}

    def attempts_by_artifact(self, conversation_id):
        out: dict[str, list[ArtifactAttempt]] = defaultdict(list)
        for t in sorted(self._attempts.values(), key=lambda t: t.created_at, reverse=True):
            if t.conversation_id == conversation_id:
                out[t.artifact_id].append(t)
        return dict(out)


# ── postgres ─────────────────────────────────────────────────────────────────
_CONV = "id, owner_id, title, defaults, summary, message_count, created_at, updated_at"
_MSG = "id, role, content, citations, meta, created_at"
_ART = "a.id, a.conversation_id, a.message_id, a.kind, a.title, a.payload, a.meta, a.created_at"
_ATT = ("id, artifact_id, conversation_id, owner_id, kind, status, score, total, state, "
        "created_at, updated_at")


def _conv(r: dict[str, Any]) -> Conversation:
    return Conversation(
        id=str(r["id"]), owner_id=str(r["owner_id"]), title=r["title"],
        defaults=ChatDefaults.model_validate(r["defaults"]), summary=r["summary"],
        message_count=r["message_count"], created_at=r["created_at"], updated_at=r["updated_at"])


def _msg(r: dict[str, Any]) -> ChatMessage:
    return ChatMessage(id=str(r["id"]), role=r["role"], content=r["content"],
                       citations=r["citations"], meta=r["meta"], created_at=r["created_at"])


def _art(r: dict[str, Any]) -> ChatArtifact:
    return ChatArtifact(id=str(r["id"]), conversation_id=str(r["conversation_id"]),
                        message_id=id_str(r["message_id"]), kind=r["kind"], title=r["title"],
                        payload=r["payload"], meta=r["meta"], created_at=r["created_at"])


def _att(r: dict[str, Any]) -> ArtifactAttempt:
    return ArtifactAttempt(
        id=str(r["id"]), artifact_id=str(r["artifact_id"]),
        conversation_id=str(r["conversation_id"]), owner_id=str(r["owner_id"]), kind=r["kind"],
        status=r["status"], score=r["score"], total=r["total"], state=r["state"],
        created_at=r["created_at"], updated_at=r["updated_at"])


@register_chat_store("postgres")
class PostgresChatStore(ChatStore):
    def __init__(self, db: Database) -> None:
        self.db = db

    @classmethod
    def from_config(cls, cfg: ChatStoreConfig, *, db: Database | None = None) -> PostgresChatStore:
        if db is None:
            raise ValueError("the postgres chat store needs database.url (DATABASE_URL)")
        return cls(db)

    # ── conversations ────────────────────────────────────────────────────────
    def create(self, conversation):
        c = conversation
        with self.db.tx() as cur:
            cur.execute(
                f"INSERT INTO chat_conversations (id, owner_id, title, defaults, summary, "
                f"created_at, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING {_CONV}",
                (as_uuid(c.id), as_uuid(c.owner_id), c.title,
                 jsonb(c.defaults.model_dump(mode="json", by_alias=True)), c.summary,
                 c.created_at, c.updated_at))
            return _conv(cur.fetchone())

    def get(self, conversation_id, *, owner_id):
        cid, oid = as_uuid(conversation_id), as_uuid(owner_id)
        if cid is None or oid is None:
            return None
        with self.db.tx() as cur:
            cur.execute(f"SELECT {_CONV} FROM chat_conversations WHERE id = %s AND owner_id = %s",
                        (cid, oid))
            row = cur.fetchone()
        return _conv(row) if row else None

    def list(self, owner_id, *, limit, cursor=None):
        oid = as_uuid(owner_id)
        if oid is None:
            return Page()
        sql = f"SELECT {_CONV} FROM chat_conversations WHERE owner_id = %s"
        params: list[Any] = [oid]
        if cursor is not None:
            sql += " AND (updated_at, id) < (%s, %s)"
            params += [cursor.ts, as_uuid(cursor.id)]
        sql += " ORDER BY updated_at DESC, id DESC LIMIT %s"
        params.append(limit + 1)
        with self.db.tx() as cur:
            cur.execute(sql, params)
            rows = [_conv(r) for r in cur.fetchall()]
        return page_of(rows, limit, _conv_key)

    def update(self, conversation_id, *, owner_id, title=None, defaults=None, summary=None):
        cid, oid = as_uuid(conversation_id), as_uuid(owner_id)
        if cid is None or oid is None:
            return None
        with self.db.tx() as cur:
            cur.execute(
                f"""UPDATE chat_conversations SET
                      title    = COALESCE(%s, title),
                      defaults = COALESCE(%s, defaults),
                      summary  = COALESCE(%s, summary),
                      updated_at = now()
                    WHERE id = %s AND owner_id = %s RETURNING {_CONV}""",
                (title, jsonb(defaults.model_dump(mode="json", by_alias=True)) if defaults else None,
                 summary, cid, oid))
            row = cur.fetchone()
        return _conv(row) if row else None

    def delete(self, conversation_id, *, owner_id):
        cid, oid = as_uuid(conversation_id), as_uuid(owner_id)
        if cid is None or oid is None:
            return False
        with self.db.tx() as cur:   # messages, artifacts, attempts cascade
            cur.execute("DELETE FROM chat_conversations WHERE id = %s AND owner_id = %s RETURNING 1",
                        (cid, oid))
            return cur.fetchone() is not None

    # ── messages ─────────────────────────────────────────────────────────────
    def add_message(self, conversation_id, message):
        cid = as_uuid(conversation_id)
        with self.db.tx() as cur:
            cur.execute("UPDATE chat_conversations SET message_count = message_count + 1, "
                        "updated_at = now() WHERE id = %s RETURNING message_count - 1 AS ord",
                        (cid,))
            row = cur.fetchone()
            if row is None:
                raise KeyError(f"unknown conversation {conversation_id!r}")
            cur.execute(
                "INSERT INTO chat_messages (id, conversation_id, ord, role, content, citations, "
                "meta, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (as_uuid(message.id), cid, row["ord"], message.role.value, message.content,
                 jsonb([c.model_dump(mode="json") for c in message.citations]),
                 jsonb(message.meta), message.created_at))
        return message

    def messages(self, conversation_id):
        with self.db.tx() as cur:
            cur.execute(f"SELECT {_MSG} FROM chat_messages WHERE conversation_id = %s "
                        "ORDER BY ord", (as_uuid(conversation_id),))
            return [_msg(r) for r in cur.fetchall()]

    def tail(self, conversation_id, *, limit):
        cid = as_uuid(conversation_id)
        with self.db.tx() as cur:
            cur.execute("SELECT message_count FROM chat_conversations WHERE id = %s", (cid,))
            head = cur.fetchone()
            total = head["message_count"] if head else 0
            if limit <= 0 or total == 0:
                return [], total
            cur.execute(f"SELECT {_MSG} FROM chat_messages WHERE conversation_id = %s "
                        "AND ord >= %s ORDER BY ord", (cid, max(0, total - limit)))
            return [_msg(r) for r in cur.fetchall()], total

    def messages_between(self, conversation_id, start, end):
        with self.db.tx() as cur:
            cur.execute(f"SELECT {_MSG} FROM chat_messages WHERE conversation_id = %s "
                        "AND ord >= %s AND ord < %s ORDER BY ord",
                        (as_uuid(conversation_id), max(0, start), max(0, end)))
            return [_msg(r) for r in cur.fetchall()]

    # ── artifacts ────────────────────────────────────────────────────────────
    def add_artifact(self, artifact):
        a = artifact
        with self.db.tx() as cur:
            cur.execute(
                "INSERT INTO chat_artifacts (id, conversation_id, message_id, kind, title, payload, "
                "meta, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (as_uuid(a.id), as_uuid(a.conversation_id), as_uuid(a.message_id), a.kind, a.title,
                 jsonb(a.payload), jsonb(a.meta), a.created_at))
        return artifact

    def get_artifact(self, artifact_id, *, owner_id):
        aid, oid = as_uuid(artifact_id), as_uuid(owner_id)
        if aid is None or oid is None:
            return None
        with self.db.tx() as cur:
            cur.execute(f"SELECT {_ART} FROM chat_artifacts a JOIN chat_conversations c "
                        "ON c.id = a.conversation_id WHERE a.id = %s AND c.owner_id = %s",
                        (aid, oid))
            row = cur.fetchone()
        return _art(row) if row else None

    def list_artifacts(self, conversation_id):
        with self.db.tx() as cur:
            cur.execute(f"SELECT {_ART} FROM chat_artifacts a WHERE a.conversation_id = %s "
                        "ORDER BY a.created_at", (as_uuid(conversation_id),))
            return [_art(r) for r in cur.fetchall()]

    def delete_artifact(self, artifact_id, *, owner_id):
        aid, oid = as_uuid(artifact_id), as_uuid(owner_id)
        if aid is None or oid is None:
            return False
        with self.db.tx() as cur:   # attempts cascade
            cur.execute("DELETE FROM chat_artifacts a USING chat_conversations c "
                        "WHERE a.id = %s AND c.id = a.conversation_id AND c.owner_id = %s "
                        "RETURNING 1", (aid, oid))
            return cur.fetchone() is not None

    # ── attempts ─────────────────────────────────────────────────────────────
    def save_attempt(self, attempt):
        t = attempt
        with self.db.tx() as cur:
            cur.execute(
                f"""INSERT INTO chat_attempts ({_ATT})
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO UPDATE SET
                      status = EXCLUDED.status, score = EXCLUDED.score, total = EXCLUDED.total,
                      state = EXCLUDED.state, updated_at = EXCLUDED.updated_at""",
                (as_uuid(t.id), as_uuid(t.artifact_id), as_uuid(t.conversation_id),
                 as_uuid(t.owner_id), t.kind, t.status, t.score, t.total, jsonb(t.state),
                 t.created_at, t.updated_at))
        return attempt

    def get_attempt(self, attempt_id, *, owner_id):
        tid, oid = as_uuid(attempt_id), as_uuid(owner_id)
        if tid is None or oid is None:
            return None
        with self.db.tx() as cur:
            cur.execute(f"SELECT {_ATT} FROM chat_attempts WHERE id = %s AND owner_id = %s",
                        (tid, oid))
            row = cur.fetchone()
        return _att(row) if row else None

    def list_attempts(self, artifact_id):
        with self.db.tx() as cur:
            cur.execute(f"SELECT {_ATT} FROM chat_attempts WHERE artifact_id = %s "
                        "ORDER BY created_at DESC", (as_uuid(artifact_id),))
            return [_att(r) for r in cur.fetchall()]

    def attempts_by_artifact(self, conversation_id):
        out: dict[str, list[ArtifactAttempt]] = defaultdict(list)
        with self.db.tx() as cur:
            cur.execute(f"SELECT {_ATT} FROM chat_attempts WHERE conversation_id = %s "
                        "ORDER BY created_at DESC", (as_uuid(conversation_id),))
            for r in cur.fetchall():
                att = _att(r)
                out[att.artifact_id].append(att)
        return dict(out)

    def counts(self):
        with self.db.tx() as cur:   # message total comes from the counters (no scan)
            cur.execute("SELECT (SELECT count(*) FROM chat_conversations) AS conversations, "
                        "(SELECT COALESCE(sum(message_count), 0) FROM chat_conversations) AS messages, "
                        "(SELECT count(*) FROM chat_artifacts) AS artifacts, "
                        "(SELECT count(*) FROM chat_attempts) AS attempts")
            return {k: int(v) for k, v in cur.fetchone().items()}

