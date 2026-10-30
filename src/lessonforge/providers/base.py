"""Abstract interfaces (ports) for every swappable component.

Consumers depend ONLY on these ABCs. Concrete adapters live under
``providers/<kind>/`` and register themselves with the registry. This is what
makes the system loosely coupled: a new backend is a new class implementing one
of these interfaces plus a one-line ``@register_*`` decorator.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..knowledge.scope import AccessScope


class UpstreamUnavailable(RuntimeError):
    """A backend (LLM, vector store…) could not be reached after its retries. The
    API maps it to HTTP 503 ``upstream.unavailable`` so clients know to retry."""


# ── LLM ──────────────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class LLMUsage:
    """Token accounting for one LLM call (when the backend reports it)."""

    tokens_in: int = 0
    tokens_out: int = 0
    model: str | None = None


@dataclass(slots=True)
class LLMResult:
    text: str
    raw: dict[str, Any] = field(default_factory=dict)
    usage: LLMUsage | None = None


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

    def stream(
        self,
        prompt: str,
        *,
        system: str | None = None,
        temperature: float | None = None,
        on_usage: Callable[[LLMUsage], None] | None = None,
    ) -> Iterator[str]:
        """Yield the completion as incremental text deltas (for streaming UIs).

        The default implementation calls :meth:`complete` and yields the whole
        result once — so every existing adapter streams correctly (if not
        incrementally) with no change. A backend with a native token stream
        overrides this to yield real deltas. Structured/JSON generation stays on
        :meth:`complete`; streaming is for free-form answer text. ``on_usage`` is
        called once with the token counts when the backend reports them."""
        result = self.complete(prompt, system=system, temperature=temperature)
        if on_usage is not None and result.usage is not None:
            on_usage(result.usage)
        yield result.text

    @abstractmethod
    def health(self) -> bool:
        """Cheap reachability check for readiness probes."""

    @property
    def supports_structured_output(self) -> bool:
        """Layer 0 of the model→domain boundary: does this backend *enforce* the
        ``json_schema`` at decode time (grammar/constrained decoding), so invalid
        output is structurally impossible? Most backends only best-effort it — the
        default is ``False``, which tells the assembler its normalize+repair layers
        are load-bearing. A backend with true guided decoding overrides this to
        ``True`` and the repair loop then almost never fires."""
        return False


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


@dataclass(slots=True)
class SparseVector:
    """A sparse (lexical) embedding: parallel term-index and weight lists. This is
    the shape Qdrant expects for a sparse vector, and what a BM25/SPLADE encoder
    produces — the lexical half of hybrid search."""

    indices: list[int] = field(default_factory=list)
    values: list[float] = field(default_factory=list)


class SparseEmbedder(ABC):
    """Lexical encoder for hybrid retrieval (e.g. BM25 via FastEmbed). Optional:
    only built and used when ``retrieval.hybrid`` is on. Kept a separate interface
    from :class:`Embedder` so a backend can supply one, both, or neither."""

    @abstractmethod
    def embed_sparse(self, texts: list[str]) -> list[SparseVector]:
        ...

    def embed_sparse_one(self, text: str) -> SparseVector:
        return self.embed_sparse([text])[0]


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
    # the lexical half, present only when hybrid ingestion is on; None keeps a
    # collection dense-only (and dense-only stores/queries keep working).
    sparse_vector: SparseVector | None = None


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
    """Vector storage with **mandatory knowledge-space isolation** (§16).

    Every point belongs to exactly one knowledge space (reserved payload key
    ``_space``, stamped by ingestion — :meth:`upsert` rejects a point without one).
    Every read and delete takes a REQUIRED keyword-only ``scope``; the adapter ANDs
    ``_space ∈ scope`` into the query *separately* from the caller's metadata
    ``where``, so no caller-side fallback that drops ``where`` can widen access. A
    call site that forgets ``scope`` fails with a ``TypeError`` immediately."""

    @abstractmethod
    def ensure_collection(self, name: str, dim: int, *, sparse: bool = False) -> None:
        """Create the collection if absent. ``sparse`` also provisions a sparse
        vector for hybrid search; a store that doesn't support sparse ignores it."""

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
        *,
        scope: AccessScope,
    ) -> list[ScoredRecord]:
        ...

    @property
    def supports_hybrid(self) -> bool:
        """Whether this store can fuse a dense + sparse query. Default ``False``:
        the base :meth:`hybrid_search` then falls back to dense-only, so hybrid
        degrades safely on stores (and old collections) without a sparse index."""
        return False

    def hybrid_search(
        self,
        name: str,
        dense_vector: list[float],
        sparse_vector: SparseVector,
        top_k: int,
        where: dict[str, Any] | None = None,
        *,
        scope: AccessScope,
    ) -> list[ScoredRecord]:
        """Fuse a dense (semantic) and sparse (lexical) query. The base
        implementation ignores the sparse side and returns the dense results, so a
        caller can always ask for hybrid and get sensible output regardless of
        backend support."""
        return self.search(name, dense_vector, top_k, where=where, scope=scope)

    def fetch(
        self, name: str, where: dict[str, Any] | None = None, *, scope: AccessScope,
        limit: int = 2000,
    ) -> list[StoredRecord]:
        """Fetch records matching an exact-match payload filter — no vector query.

        This is the dereference primitive behind multi-granularity retrieval: given
        a narrow hit's hierarchy ids, fetch every chunk of its section or chapter so
        the passage can be reassembled. The default pages through :meth:`scroll` and
        filters in Python (correct on any store); a backend with a native payload
        filter (e.g. Qdrant's scroll filter) overrides this for efficiency."""
        out: list[StoredRecord] = []
        offset: str | None = None
        while len(out) < limit:
            page, offset = self.scroll(name, limit=512, offset=offset, scope=scope)
            if not page:
                break
            for r in page:
                if where is None or all(r.payload.get(k) == v for k, v in where.items()):
                    out.append(r)
                    if len(out) >= limit:
                        break
            if offset is None:
                break
        return out

    # ── read-only browse / curation (admin surfaces, never the hot path) ──────
    @abstractmethod
    def count(self, name: str, *, scope: AccessScope) -> int:
        """Points in ``name`` within ``scope``; ``0`` if the collection does not exist."""

    @abstractmethod
    def scroll(
        self, name: str, *, limit: int, offset: str | None = None, scope: AccessScope
    ) -> tuple[list[StoredRecord], str | None]:
        """Page through a collection within ``scope``. Returns ``(records,
        next_offset)``; pass ``next_offset`` back to fetch the following page,
        ``None`` when exhausted. ``offset`` is an opaque token. Empty for a missing
        collection."""

    @abstractmethod
    def delete(self, name: str, ids: list[str], *, scope: AccessScope) -> int:
        """Delete points by their (source) id — only those inside ``scope``.
        Returns how many ids were requested; a no-op for a missing collection."""

    @abstractmethod
    def delete_where(self, name: str, where: dict[str, Any], *, scope: AccessScope) -> None:
        """Delete every point matching ``where`` inside ``scope`` (one document's
        chunks by ``doc_id``; a whole archived space by ``_space``)."""

    def backfill_space(self, name: str, space: str) -> int:
        """Stamp ``_space = space`` on every point that has none (one-off upgrade of
        a corpus ingested before knowledge spaces). Returns points touched, or
        ``-1`` when the backend cannot count them."""
        raise NotImplementedError

    @abstractmethod
    def health(self) -> bool:
        ...
