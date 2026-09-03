"""Domain models for the QA chatbot: conversations, messages, citations.

Pure data (Pydantic) — no I/O, no retrieval, no LLM. The :class:`ChatStore`
persists these; the pipeline produces them. Keeping them free of behaviour is
what lets the store, the API, and the pipeline all depend on the same shapes
without depending on each other.

Provenance is first-class, exactly as in the lesson pipeline: an assistant
message carries the :class:`Citation` list it was grounded in, each holding the
*actual retrieved chunk text* so the UI can preview a reference with no extra
round-trip, and never invent one.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


def _now() -> datetime:
    return datetime.now(UTC)


def _new_id() -> str:
    return uuid.uuid4().hex


class Role(str, Enum):
    """Who authored a message. ``system`` is reserved for future use (e.g. a
    pinned instruction turn); the pipeline only ever writes ``user``/``assistant``."""

    user = "user"
    assistant = "assistant"
    system = "system"


class FilterMode(str, Enum):
    """The two retrieval modes. ``scoped`` applies the conversation's default tag
    filters (grade/subject/class/…); ``broad`` ignores them and searches the
    whole corpus. A message may override the conversation default with either."""

    scoped = "scoped"
    broad = "broad"


class Citation(BaseModel):
    """One grounded reference under an assistant answer. ``n`` is the 1-based
    marker the answer cites (``[n]``); ``text`` is the exact retrieved chunk so
    the UI previews it client-side."""

    n: int
    source: str
    collection: str
    text: str
    score: float = 0.0
    metadata: dict[str, Any] = Field(default_factory=dict)


class ChatMessage(BaseModel):
    id: str = Field(default_factory=_new_id)
    role: Role
    content: str
    citations: list[Citation] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_now)
    # free-form per-message extras (mode used, transformed queries, timings…),
    # kept open so the pipeline can record diagnostics without a schema change.
    meta: dict[str, Any] = Field(default_factory=dict)


class ChatDefaults(BaseModel):
    """A conversation's default retrieval scope. The named fields are the common
    tags; ``tags`` carries any other metadata key the corpus uses, so a curator
    can filter on fields we didn't anticipate — without a code change."""

    mode: FilterMode = FilterMode.scoped
    grade: int | None = None
    subject: str | None = None
    class_: str | None = Field(default=None, alias="class")
    collections: list[str] | None = None  # None → use the config default set
    tags: dict[str, Any] = Field(default_factory=dict)

    model_config = {"populate_by_name": True}

    def filters(self, filter_fields: list[str]) -> dict[str, Any]:
        """The metadata filter dict for *scoped* mode: the configured
        ``filter_fields`` that have a value here, plus any explicit ``tags``.
        ``class_`` maps back to the ``class`` payload key."""
        named = {"grade": self.grade, "subject": self.subject, "class": self.class_}
        where = {f: named[f] for f in filter_fields if named.get(f) is not None}
        where.update({k: v for k, v in self.tags.items() if v is not None})
        return where


class Conversation(BaseModel):
    id: str = Field(default_factory=_new_id)
    owner_id: str = "default"
    title: str = "New chat"
    defaults: ChatDefaults = Field(default_factory=ChatDefaults)
    summary: str = ""  # rolling long-context summary of older turns
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


# ── API DTOs ──────────────────────────────────────────────────────────────────
class CreateConversationRequest(BaseModel):
    title: str | None = None
    defaults: ChatDefaults = Field(default_factory=ChatDefaults)
    owner_id: str = "default"


class UpdateConversationRequest(BaseModel):
    """Partial update: any field left ``None`` is untouched."""

    title: str | None = None
    defaults: ChatDefaults | None = None


class ChatMessageRequest(BaseModel):
    """A user turn. ``mode`` / ``filters`` override the conversation defaults for
    this one message only (the two-modes control); omit to use the defaults."""

    message: str = Field(min_length=1)
    mode: FilterMode | None = None
    filters: dict[str, Any] | None = None
