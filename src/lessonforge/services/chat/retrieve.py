"""ChatRetriever — multi-query, multi-collection retrieval with RRF fusion.

Built on the shared, interface-only :class:`Retriever` (hybrid dense⊕BM25 +
rerank is already solved there) — this layer only adds what QA needs:

1. run every transformed query against every configured collection,
2. **reciprocal-rank fuse** the result lists (recall across phrasings/sources),
3. de-duplicate, then **rerank** the fused union against the standalone question
   (precision), and
4. honour the two modes — *scoped* applies the tag filters with the same lenient
   fallback as :class:`GroundingRetriever`; *broad* searches unfiltered.

Returns provenance-carrying :class:`ScoredChunk` objects; the context builder
turns the budgeted subset into numbered :class:`Citation`s.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ...config import ChatRetrievalConfig
from ...domain.chat import FilterMode
from ...rag.documents import COLLECTION_KEY, SOURCE_KEY, TEXT_KEY, Collection
from ...rag.retriever import Retriever
from .transform import TransformedQuery

# Standard RRF constant; dampens the influence of exact rank so lists combine
# smoothly. 60 is the value from the original Cormack et al. RRF paper.
_RRF_K = 60

_RESERVED = frozenset({TEXT_KEY, SOURCE_KEY, COLLECTION_KEY})


@dataclass(slots=True)
class ScoredChunk:
    text: str
    source: str
    collection: str
    score: float
    metadata: dict[str, Any] = field(default_factory=dict)


class ChatRetriever:
    def __init__(self, retriever: Retriever, config: ChatRetrievalConfig) -> None:
        self.retriever = retriever
        self.config = config

    def retrieve(
        self,
        transformed: TransformedQuery,
        *,
        mode: FilterMode,
        filters: dict[str, Any] | None = None,
        collections: list[str] | None = None,
    ) -> list[ScoredChunk]:
        cols = self._collections(collections)
        where = self._where(mode, filters)
        fused = self._gather(cols, transformed.queries, where)
        # Lenient fallback: a scoped query that finds nothing broadens to
        # unfiltered rather than answering from an empty context.
        if not fused and where:
            fused = self._gather(cols, transformed.queries, None)
        if not fused:
            return []
        return self._rerank(transformed.standalone, fused)

    # ── internals ──────────────────────────────────────────────────────────────
    def _collections(self, override: list[str] | None) -> list[str]:
        names = override if override is not None else self.config.collections
        valid: list[str] = []
        for n in names:
            try:
                valid.append(Collection(n).value)  # reject unknown names, don't crash
            except ValueError:
                continue
        return valid

    def _where(self, mode: FilterMode, filters: dict[str, Any] | None) -> dict[str, Any] | None:
        if mode is FilterMode.broad:
            return None
        where = {k: v for k, v in (filters or {}).items() if v is not None}
        return where or None

    def _gather(
        self, collections: list[str], queries: list[str], where: dict[str, Any] | None
    ) -> dict[str, tuple[ScoredChunk, float]]:
        """Retrieve every (collection × query) list and RRF-fuse into a map keyed
        by chunk identity, carrying the fused score. Per-list failures are
        swallowed so one bad collection never sinks the answer."""
        fused: dict[str, tuple[ScoredChunk, float]] = {}
        top_n = self.config.per_collection_top_n
        for collection in collections:
            for query in queries:
                try:
                    hits = self.retriever.retrieve(collection, query, where=where, top_n=top_n)
                except Exception:  # noqa: S112 - one bad collection must not sink the answer
                    continue
                for rank, hit in enumerate(hits):
                    chunk = self._to_chunk(hit, collection)
                    key = self._key(chunk)
                    contribution = 1.0 / (_RRF_K + rank)
                    if key in fused:
                        existing, score = fused[key]
                        fused[key] = (existing, score + contribution)
                    else:
                        fused[key] = (chunk, contribution)
        return fused

    def _rerank(
        self, query: str, fused: dict[str, tuple[ScoredChunk, float]]
    ) -> list[ScoredChunk]:
        """Rerank the fused union against the standalone question, then keep
        ``rerank_top_n``. Falls back to the fused (RRF) order if reranking fails."""
        # order fused candidates by RRF first so a reranker cap still sees the best
        candidates = [c for c, _ in sorted(fused.values(), key=lambda t: t[1], reverse=True)]
        try:
            ranked = self.retriever.reranker.rerank(
                query, [c.text for c in candidates], top_n=self.config.rerank_top_n
            )
        except Exception:
            return candidates[: self.config.rerank_top_n]
        out: list[ScoredChunk] = []
        for r in ranked:
            chunk = candidates[r.index]
            out.append(
                ScoredChunk(
                    text=chunk.text, source=chunk.source, collection=chunk.collection,
                    score=float(r.score), metadata=chunk.metadata,
                )
            )
        return out

    @staticmethod
    def _to_chunk(hit: Any, collection: str) -> ScoredChunk:
        payload = hit.payload or {}
        metadata = {
            k: v for k, v in payload.items() if k not in _RESERVED and not k.startswith("_")
        }
        return ScoredChunk(
            text=hit.text,
            source=str(payload.get(SOURCE_KEY, "") or "?"),
            collection=str(payload.get(COLLECTION_KEY, collection)),
            score=float(hit.score),
            metadata=metadata,
        )

    @staticmethod
    def _key(chunk: ScoredChunk) -> str:
        # identity = collection + exact text; the same passage retrieved via two
        # queries fuses instead of appearing twice.
        return f"{chunk.collection}\x00{chunk.text}"
