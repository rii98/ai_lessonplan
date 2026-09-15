"""Domain models for the document/version memory layer.

A teacher's generated work is no longer ephemeral: every lesson (and, later, unit)
is a :class:`StoredDocument` with an append-only chain of :class:`DocumentVersion`
snapshots. A version is created on each state transition — the first generation, an
accepted refine, a manual edit — and the document's ``head_version_id`` points at
the current one. That single design buys the four things a teacher asks for:

- **revisit** — load the head, or any past version, by id;
- **edit**    — a refine/edit appends a new version (never mutates the old);
- **undo**    — move the head pointer back to a version's ``parent_id``;
- **audit**   — each version records what produced it (origin, instruction, diff,
  grounding sources, critique scores).

Pure data (Pydantic), no I/O — mirrors :mod:`lessonforge.domain.chat`. The
:class:`~lessonforge.services.documents.store.DocumentStore` persists these; the
:class:`~lessonforge.services.documents.service.DocumentService` produces them.
The version ``snapshot`` is the document's own JSON (an LDD today, a unit later),
kept schema-agnostic so this layer never couples to a specific source-of-truth.
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


class DocumentKind(str, Enum):
    """What a stored document holds. ``lesson`` snapshots an LDD; ``unit`` (Phase 4)
    snapshots a multi-day unit. The store is agnostic — the kind is metadata for
    listing and for choosing how to rehydrate the snapshot."""

    lesson = "lesson"
    unit = "unit"


class VersionOrigin(str, Enum):
    """What produced a version — the audit trail's verb."""

    generate = "generate"   # the first, AI-generated baseline
    refine = "refine"       # an accepted AI refine (carries instruction + diff)
    manual = "manual"       # a teacher's direct edit


class DocumentVersion(BaseModel):
    """One immutable snapshot in a document's history. Never edited after creation;
    a change appends a new version whose ``parent_id`` is the version it derived
    from (``None`` for the first). ``snapshot`` is the full document JSON at this
    point — self-contained, so any version rehydrates without replaying the chain."""

    id: str = Field(default_factory=_new_id)
    document_id: str
    parent_id: str | None = None
    origin: VersionOrigin = VersionOrigin.generate
    snapshot: dict[str, Any]
    # audit trail — why this version exists (empty for a plain generate).
    instruction: str = ""
    diff: dict[str, Any] = Field(default_factory=dict)
    grounding_sources: list[str] = Field(default_factory=list)
    scores: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_now)


class StoredDocument(BaseModel):
    """The document header: identity, ownership, and a pointer to the current
    version. The content lives in the versions; this row is what ``list`` returns
    and what the head pointer moves over on undo."""

    id: str = Field(default_factory=_new_id)
    owner_id: str = "default"
    kind: DocumentKind = DocumentKind.lesson
    title: str = "Untitled"
    # light, queryable framing so a teacher can find work later without loading
    # every snapshot; optional so a non-lesson document need not supply them.
    grade: int | None = None
    subject: str | None = None
    head_version_id: str | None = None  # None until the first version is added
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
