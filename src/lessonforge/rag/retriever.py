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

from dataclasses import dataclass
from typing import Any

from ..providers.base import Embedder, Reranker, SparseEmbedder, VectorStore


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
    ) -> None:
        self.embedder = embedder
        self.vector_store = vector_store
        self.reranker = reranker
        self.sparse_embedder = sparse_embedder
        self.hybrid = hybrid
        self.top_k = top_k
        self.rerank_top_n = rerank_top_n
        self.text_field = text_field

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
        return [
            RetrievedChunk(
                text=r.document,
                score=r.score,
                payload=hits[r.index].payload,
            )
            for r in ranked
        ]
