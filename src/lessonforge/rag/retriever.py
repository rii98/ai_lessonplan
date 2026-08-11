"""Retriever: embed → vector search → rerank.

Depends ONLY on the Embedder, VectorStore, and Reranker interfaces — so any of
the three can be swapped via config with zero changes here. This module is the
clearest demonstration of the loose-coupling contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..providers.base import Embedder, Reranker, VectorStore


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
        top_k: int = 20,
        rerank_top_n: int = 5,
        text_field: str = "text",
    ) -> None:
        self.embedder = embedder
        self.vector_store = vector_store
        self.reranker = reranker
        self.top_k = top_k
        self.rerank_top_n = rerank_top_n
        self.text_field = text_field

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
