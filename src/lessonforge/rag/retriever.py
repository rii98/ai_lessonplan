"""Retriever: embed → (hybrid) vector search → rerank.

Depends ONLY on the Embedder, SparseEmbedder, VectorStore, and Reranker
interfaces — so any of them can be swapped via config with zero changes here. This
module is the clearest demonstration of the loose-coupling contract.

Retrieval is hybrid when configured (``retrieval.hybrid``) AND a sparse embedder is
wired AND the store advertises ``supports_hybrid``: the query is embedded densely
(semantic) and sparsely (BM25/lexical), the store fuses them (RRF), and the fused
candidates are reranked. Any of those missing degrades cleanly to dense-only — so
the same call works before a corpus is re-ingested for hybrid.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal

from ..providers.base import Embedder, Reranker, SparseEmbedder, VectorStore
from .documents import (
    CHAPTER_KEY,
    CHUNK_INDEX_KEY,
    DOC_ID_KEY,
    HEADING_PATH_KEY,
    SOURCE_KEY,
    TEXT_KEY,
)

# How much of the corpus a retrieval returns per hit:
#   narrow  — the reranked child chunks themselves (a definition, one fact)
#   section — each hit expanded to its whole section (a procedure, a phase)
#   broad   — each hit expanded to its whole chapter (a unit arc, an overview)
Granularity = Literal["narrow", "section", "broad"]

# Shadow-logging channel for reranker scores. Emits one debug record per retrieval
# with the per-chunk scores, so a relevance floor can be calibrated on the real
# distribution *before* it is turned on to act (see the grounding quality gate).
# Silent at the default level — enable with logging.getLogger(
# "lessonforge.rag.retriever.scores").setLevel(logging.DEBUG).
_score_log = logging.getLogger(__name__ + ".scores")


@dataclass(slots=True)
class RetrievedChunk:
    text: str
    score: float
    payload: dict[str, Any]


class Retriever:
    def __init__(
        self,
        *,
        embedder: Embedder,
        vector_store: VectorStore,
        reranker: Reranker,
        sparse_embedder: SparseEmbedder | None = None,
        hybrid: bool = False,
        top_k: int = 20,
        rerank_top_n: int = 5,
        text_field: str = "text",
        assembly_max_chars: int = 8000,
    ) -> None:
        self.embedder = embedder
        self.vector_store = vector_store
        self.reranker = reranker
        self.sparse_embedder = sparse_embedder
        self.hybrid = hybrid
        self.top_k = top_k
        self.rerank_top_n = rerank_top_n
        self.text_field = text_field
        # Per-hit budget when a section/chapter is reassembled from its chunks, so
        # a broad expansion can't blow the prompt. The assembled passage is capped
        # here; narrow retrieval is unaffected.
        self.assembly_max_chars = assembly_max_chars

    @property
    def hybrid_active(self) -> bool:
        """Hybrid runs only when asked for, wired, and supported — otherwise dense."""
        return bool(
            self.hybrid
            and self.sparse_embedder is not None
            and self.vector_store.supports_hybrid
        )

    def retrieve(
        self,
        collection: str,
        query: str,
        *,
        where: dict[str, Any] | None = None,
        top_n: int | None = None,
        granularity: Granularity = "narrow",
    ) -> list[RetrievedChunk]:
        top_n = top_n or self.rerank_top_n
        vector = self.embedder.embed_one(query)
        if self.hybrid_active:
            sparse = self.sparse_embedder.embed_sparse_one(query)  # type: ignore[union-attr]
            hits = self.vector_store.hybrid_search(collection, vector, sparse, self.top_k, where=where)
        else:
            hits = self.vector_store.search(collection, vector, self.top_k, where=where)
        if not hits:
            return []

        documents = [h.payload.get(self.text_field, "") for h in hits]
        ranked = self.reranker.rerank(query, documents, top_n=top_n)
        chunks = [
            RetrievedChunk(
                text=r.document,
                score=r.score,
                payload=hits[r.index].payload,
            )
            for r in ranked
        ]
        self._log_scores(collection, query, chunks)
        if granularity == "narrow":
            return chunks
        return self._expand(collection, chunks, granularity)

    # ── small-to-big: expand a narrow hit to its section or chapter ────────────
    def _expand(
        self, collection: str, chunks: list[RetrievedChunk], granularity: Granularity
    ) -> list[RetrievedChunk]:
        """Dereference each narrow hit to the larger unit it belongs to.

        ``section`` groups by (doc_id, heading_path); ``broad`` by (doc_id,
        chapter). Every chunk of that unit is fetched, ordered by chunk_index, and
        concatenated (capped at ``assembly_max_chars``) into a single reassembled
        passage that keeps the best reranker score and the source of its group.
        Hits with no hierarchy (a flat source, or a store that dropped the ids)
        pass through unchanged, and each unit is emitted once even if several of
        its chunks were retrieved — so ranking order is preserved without dupes."""
        group_key = HEADING_PATH_KEY if granularity == "section" else CHAPTER_KEY
        out: list[RetrievedChunk] = []
        seen: set[tuple[Any, Any]] = set()
        for chunk in chunks:
            doc_id = chunk.payload.get(DOC_ID_KEY)
            unit = chunk.payload.get(group_key)
            if doc_id is None or unit is None:
                out.append(chunk)  # nothing to expand into; keep the narrow hit
                continue
            key = (doc_id, unit)
            if key in seen:
                continue  # this section/chapter already assembled from a higher hit
            seen.add(key)
            assembled = self._assemble(collection, doc_id, group_key, unit, chunk)
            out.append(assembled or chunk)
        return out

    def _assemble(
        self,
        collection: str,
        doc_id: Any,
        group_key: str,
        unit: Any,
        seed: RetrievedChunk,
    ) -> RetrievedChunk | None:
        """Fetch and stitch every chunk of one section/chapter into one passage."""
        try:
            records = self.vector_store.fetch(
                collection, {DOC_ID_KEY: doc_id, group_key: unit}
            )
        except Exception:
            return None  # store can't fetch → caller falls back to the narrow hit
        if not records:
            return None
        records.sort(key=lambda r: r.payload.get(CHUNK_INDEX_KEY, 0))
        parts: list[str] = []
        total = 0
        for r in records:
            text = r.payload.get(TEXT_KEY, "")
            if not text:
                continue
            if total + len(text) > self.assembly_max_chars and parts:
                break  # budget spent; stop before overflowing the prompt
            parts.append(text)
            total += len(text)
        if not parts:
            return None
        # Provenance and hierarchy come from the seed (the reranked hit); the score
        # is the seed's, so an expanded unit ranks where its best chunk ranked.
        payload = {**seed.payload, SOURCE_KEY: seed.payload.get(SOURCE_KEY, "?")}
        return RetrievedChunk(text="\n\n".join(parts), score=seed.score, payload=payload)

    def _log_scores(
        self, collection: str, query: str, chunks: list[RetrievedChunk]
    ) -> None:
        """Shadow-log the reranker scores of a retrieval (no effect on results).

        Cheap and lazy: skipped entirely unless the score logger is at DEBUG, so
        it costs nothing in production and gathers the distribution needed to
        calibrate a relevance floor when a curator turns it on."""
        if not chunks or not _score_log.isEnabledFor(logging.DEBUG):
            return
        scores = ", ".join(f"{c.score:.4f}" for c in chunks)
        _score_log.debug(
            "rerank collection=%s n=%d top=%.4f scores=[%s] query=%r",
            collection, len(chunks), chunks[0].score, scores, query[:120],
        )
