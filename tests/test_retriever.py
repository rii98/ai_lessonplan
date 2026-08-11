"""Retriever wires embedder + store + reranker through interfaces only."""

from __future__ import annotations

from lessonforge.providers.base import VectorRecord
from lessonforge.rag.retriever import Retriever


def test_retrieve_embeds_searches_and_reranks(fake_embedder, fake_store, fake_reranker):
    fake_store.ensure_collection("pedagogical", fake_embedder.dim)
    docs = [
        "Rice plant is a producer in the paddy field",
        "The capital of France is Paris",
        "A goat is a consumer that eats grass",
    ]
    fake_store.upsert(
        "pedagogical",
        [VectorRecord(id=str(i), vector=fake_embedder.embed_one(t), payload={"text": t})
         for i, t in enumerate(docs)],
    )

    r = Retriever(
        embedder=fake_embedder, vector_store=fake_store, reranker=fake_reranker,
        top_k=10, rerank_top_n=2,
    )
    out = r.retrieve("pedagogical", "producer in a paddy field")

    assert len(out) == 2
    # keyword-overlap reranker should float the paddy-field doc to the top
    assert "paddy field" in out[0].text


def test_retrieve_empty_collection_returns_empty(fake_embedder, fake_store, fake_reranker):
    fake_store.ensure_collection("pedagogical", fake_embedder.dim)
    r = Retriever(embedder=fake_embedder, vector_store=fake_store, reranker=fake_reranker)
    assert r.retrieve("pedagogical", "anything") == []
