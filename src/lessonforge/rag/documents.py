"""Corpus primitives for grounding: collections, source documents, and chunks.

A :class:`Document` is one source unit (a curriculum outcome, a misconception
note, an exemplar lesson section). A :class:`Chunk` is the embeddable unit a
document is split into. Both carry free-form ``metadata`` (grade, subject,
standard, language, source) that becomes the vector-store payload — the same
keys are what metadata filters match on at search time.

Provenance is first-class: every chunk knows its human-readable ``source`` label,
which flows into the LDD's ``quality.grounding_sources`` so a teacher can see and
trust where a grounded fact came from.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Collection(str, Enum):
    """The v1 grounding collections (see SRD § 06). Each is an independent
    logical corpus in the vector store; retrieval queries them by name."""

    curriculum = "curriculum"        # CDC/NEB standards & learning outcomes
    pedagogical = "pedagogical"      # misconceptions, 5E strategies — the moat
    exemplar = "exemplar"            # hand-picked reference lessons (few-shot)
    local_context = "local_context"  # paddy field, goat, monsoon… local hooks

    @property
    def description(self) -> str:
        """One-line, curator-facing explanation of what belongs here — surfaced
        by the corpus-management UI so a non-technical author picks the right
        collection without reading the SRD."""
        return _COLLECTION_DESCRIPTIONS[self]


_COLLECTION_DESCRIPTIONS: dict[Collection, str] = {
    Collection.curriculum: (
        "CDC/NEB standards and learning outcomes — what students must learn at "
        "each grade. Add official curriculum statements and objectives here."
    ),
    Collection.pedagogical: (
        "Teaching know-how — common misconceptions, 5E strategies, and concrete "
        "teaching moves. This is the moat: the richer it is, the less generic "
        "every generated lesson becomes."
    ),
    Collection.exemplar: (
        "Hand-picked reference lessons used as few-shot examples of what a great "
        "lesson looks like. Add whole strong lessons, not fragments."
    ),
    Collection.local_context: (
        "Local hooks that make lessons concrete for Nepali classrooms — paddy "
        "fields, goats, the monsoon, local festivals and places."
    ),
}


# Payload keys the vector-store record carries. `text` is what gets reranked;
# the rest are filterable metadata + provenance.
TEXT_KEY = "text"
SOURCE_KEY = "source"
COLLECTION_KEY = "collection"


@dataclass(slots=True)
class Document:
    """A source unit before chunking."""

    id: str
    text: str
    source: str                       # human-readable provenance label
    metadata: dict[str, Any] = field(default_factory=dict)  # grade, subject, standard, language…


@dataclass(slots=True)
class Chunk:
    """An embeddable unit derived from a Document, tagged with its collection."""

    id: str
    text: str
    collection: Collection
    source: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def payload(self) -> dict[str, Any]:
        """Flatten to the vector-store payload. Metadata sits at the top level so
        grade/subject/standard are directly filterable."""
        return {
            **self.metadata,
            TEXT_KEY: self.text,
            SOURCE_KEY: self.source,
            COLLECTION_KEY: self.collection.value,
        }


def content_id(collection: Collection, text: str, metadata: dict[str, Any]) -> str:
    """Stable, content-derived chunk id → ingestion is idempotent (re-ingesting
    the same content upserts the same point instead of duplicating it)."""
    h = hashlib.sha256()
    h.update(collection.value.encode())
    h.update(b"\x00")
    h.update(text.strip().encode())
    for k in sorted(metadata):
        h.update(f"\x00{k}={metadata[k]}".encode())
    return f"{collection.value}:{h.hexdigest()[:16]}"
