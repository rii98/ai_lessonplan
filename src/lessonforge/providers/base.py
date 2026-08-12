"""Abstract interfaces (ports) for every swappable component.

Consumers depend ONLY on these ABCs. Concrete adapters live under
``providers/<kind>/`` and register themselves with the registry. This is what
makes the system loosely coupled: a new backend is a new class implementing one
of these interfaces plus a one-line ``@register_*`` decorator.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


# ── LLM ──────────────────────────────────────────────────────────────────────
@dataclass(slots=True)
class LLMResult:
    text: str
    raw: dict[str, Any] = field(default_factory=dict)


class LLMClient(ABC):
    """Text/JSON completion backend (Ollama, OpenAI-compatible, ...)."""

    @abstractmethod
    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        json_schema: dict[str, Any] | None = None,
        temperature: float | None = None,
    ) -> LLMResult:
        """Return a completion. If ``json_schema`` is given, the backend is asked
        to emit JSON conforming to it (best effort; caller still validates)."""

    @abstractmethod
    def health(self) -> bool:
        """Cheap reachability check for readiness probes."""


# ── Embeddings ───────────────────────────────────────────────────────────────
class Embedder(ABC):
    @property
    @abstractmethod
    def dim(self) -> int:
        """Vector dimensionality (needed to create vector-store collections)."""

    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]:
        ...

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]


# ── Reranking ────────────────────────────────────────────────────────────────
@dataclass(slots=True)
class RerankResult:
    index: int  # index into the input documents list
    score: float
    document: str


class Reranker(ABC):
    @abstractmethod
    def rerank(self, query: str, documents: list[str], top_n: int) -> list[RerankResult]:
        """Return the top_n documents ordered by relevance (highest first)."""


# ── Vector store ─────────────────────────────────────────────────────────────
@dataclass(slots=True)
class VectorRecord:
    id: str
    vector: list[float]
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ScoredRecord:
    id: str
    score: float
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class StoredRecord:
    """A point read back from the store for browsing/curation (no score)."""

    id: str
    payload: dict[str, Any] = field(default_factory=dict)


class VectorStore(ABC):
    @abstractmethod
    def ensure_collection(self, name: str, dim: int) -> None:
        ...

    @abstractmethod
    def upsert(self, name: str, records: list[VectorRecord]) -> None:
        ...

    @abstractmethod
    def search(
        self,
        name: str,
        vector: list[float],
        top_k: int,
        where: dict[str, Any] | None = None,
    ) -> list[ScoredRecord]:
        ...

    # ── read-only browse / curation (admin surfaces, never the hot path) ──────
    @abstractmethod
    def count(self, name: str) -> int:
        """Number of points in ``name``; ``0`` if the collection does not exist."""

    @abstractmethod
    def scroll(
        self, name: str, *, limit: int, offset: str | None = None
    ) -> tuple[list[StoredRecord], str | None]:
        """Page through a collection. Returns ``(records, next_offset)``; pass
        ``next_offset`` back to fetch the following page, ``None`` when exhausted.
        ``offset`` is an opaque token (not a row number). Empty for a missing
        collection."""

    @abstractmethod
    def delete(self, name: str, ids: list[str]) -> int:
        """Delete points by their (source) id. Returns how many ids were
        requested; a no-op for a missing collection."""

    @abstractmethod
    def health(self) -> bool:
        ...
