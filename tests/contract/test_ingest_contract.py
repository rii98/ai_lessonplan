"""Contract test for the live grounding path: seed corpus → real FastEmbed
embeddings → real Qdrant collections → metadata-filtered retrieval.

Skipped unless RUN_INTEGRATION=1 and Qdrant is reachable. This is what proves the
ingestion + retrieval pipeline honors its interfaces against real backends, not
just fakes. Collections are namespaced with a test prefix and dropped afterward.
"""

from __future__ import annotations

import os
import uuid

import pytest

from lessonforge.config import load_settings
from lessonforge.providers.registry import build_embedder, build_vector_store
from lessonforge.providers.reranking.noop import NoopReranker
from lessonforge.rag.documents import Collection
from lessonforge.rag.grounding import GroundingRetriever
from lessonforge.rag.ingest import Ingestor, ingest_seed
from lessonforge.rag.retriever import Retriever

pytestmark = pytest.mark.integration


@pytest.mark.skipif(os.environ.get("RUN_INTEGRATION") != "1", reason="integration disabled")
def test_seed_ingest_and_grounded_retrieval_live():
    settings = load_settings()
    # isolate this run's collections behind a unique prefix
    settings.vector_store.collection_prefix = f"lftest_{uuid.uuid4().hex[:8]}"
    store = build_vector_store(settings.vector_store)
    if not store.health():
        pytest.skip("Qdrant not reachable at configured url")
    embedder = build_embedder(settings.embedding)

    ingestor = Ingestor(embedder=embedder, vector_store=store)
    try:
        report = ingest_seed(ingestor)
        assert report.chunks > 0

        retriever = Retriever(
            embedder=embedder, vector_store=store, reranker=NoopReranker(),
            top_k=settings.retrieval.top_k, rerank_top_n=5,
        )
        grounder = GroundingRetriever(retriever, settings.grounding)
        bundle = grounder.ground(
            query="classify biotic and abiotic components of the environment",
            grade=6, subject="Science",
        )
        assert not bundle.is_empty
        assert bundle.sources  # real provenance came back
        # the environment topic should surface a relevant chunk
        joined = " ".join(
            c.text.lower() for chunks in bundle.chunks.values() for c in chunks
        )
        assert "biotic" in joined or "abiotic" in joined
    finally:
        # best-effort cleanup of this run's collections
        client = store._c()
        for c in Collection:
            full = store._name(c.value)
            if client.collection_exists(full):
                client.delete_collection(full)
