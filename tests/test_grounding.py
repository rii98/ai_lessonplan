"""GroundingRetriever: multi-collection retrieval, metadata filtering, lenient
fallback, provenance de-duplication, graceful degradation."""

from __future__ import annotations

from lessonforge.config import GroundingConfig
from lessonforge.rag.documents import Collection, Document
from lessonforge.rag.grounding import GroundingRetriever
from lessonforge.rag.ingest import Ingestor
from lessonforge.rag.retriever import Retriever


def _grounder(fake_embedder, fake_store, fake_reranker, config=None) -> GroundingRetriever:
    retriever = Retriever(embedder=fake_embedder, vector_store=fake_store,
                          reranker=fake_reranker, top_k=20, rerank_top_n=5)
    return GroundingRetriever(retriever, config or GroundingConfig())


def _seed(fake_embedder, fake_store):
    ing = Ingestor(embedder=fake_embedder, vector_store=fake_store)
    ing.ingest_documents(Collection.curriculum, [
        Document(id="c1", text="classify biotic and abiotic components", source="cur",
                 metadata={"grade": 6, "subject": "Science"}),
    ])
    ing.ingest_documents(Collection.pedagogical, [
        Document(id="p1", text="clouds move but are not living misconception", source="ped6",
                 metadata={"grade": 6, "subject": "Science"}),
        Document(id="p2", text="grade seven sound vibration strategy", source="ped7",
                 metadata={"grade": 7, "subject": "Science"}),
    ])


def test_ground_pulls_from_multiple_collections_with_provenance(
    fake_embedder, fake_store, fake_reranker
):
    _seed(fake_embedder, fake_store)
    g = _grounder(fake_embedder, fake_store, fake_reranker)
    bundle = g.ground(query="biotic abiotic components", grade=6, subject="Science")
    assert bundle.chunks["curriculum"]
    assert bundle.chunks["pedagogical"]
    assert "cur" in bundle.sources and "ped6" in bundle.sources
    # provenance is de-duplicated and preserves first-seen order
    assert len(bundle.sources) == len(set(bundle.sources))


def test_metadata_filter_excludes_other_grades(fake_embedder, fake_store, fake_reranker):
    _seed(fake_embedder, fake_store)
    g = _grounder(fake_embedder, fake_store, fake_reranker)
    bundle = g.ground(query="anything", grade=6, subject="Science")
    ped_sources = {c.payload["source"] for c in bundle.chunks["pedagogical"]}
    assert "ped6" in ped_sources
    assert "ped7" not in ped_sources  # grade-7 filtered out


def test_lenient_fallback_when_filter_matches_nothing(
    fake_embedder, fake_store, fake_reranker
):
    _seed(fake_embedder, fake_store)
    g = _grounder(fake_embedder, fake_store, fake_reranker)
    # grade 9 matches nothing; fallback should broaden and still return chunks
    bundle = g.ground(query="components", grade=9, subject="Science")
    assert bundle.chunks["curriculum"], "expected lenient fallback to return context"


def test_prompt_context_and_empty_bundle(fake_embedder, fake_store, fake_reranker):
    g = _grounder(fake_embedder, fake_store, fake_reranker)  # empty store
    bundle = g.ground(query="x", grade=6, subject="Science")
    assert bundle.is_empty
    assert "No retrieved context" in bundle.as_prompt_context()


def test_unknown_collection_name_is_skipped(fake_embedder, fake_store, fake_reranker):
    _seed(fake_embedder, fake_store)
    cfg = GroundingConfig(collections=["curriculum", "not_a_collection"])
    g = _grounder(fake_embedder, fake_store, fake_reranker, cfg)
    bundle = g.ground(query="components", grade=6, subject="Science")
    assert "curriculum" in bundle.chunks
    assert "not_a_collection" not in bundle.chunks


def test_retrieval_error_degrades_to_empty(fake_embedder, fake_store, fake_reranker):
    """A blowing-up retriever must not break grounding — it yields no context."""
    g = _grounder(fake_embedder, fake_store, fake_reranker)

    def boom(*a, **k):
        raise RuntimeError("store down")

    g.retriever.retrieve = boom  # type: ignore[method-assign]
    bundle = g.ground(query="x", grade=6, subject="Science")
    assert bundle.is_empty


def test_no_filter_fields_queries_unfiltered(fake_embedder, fake_store, fake_reranker):
    _seed(fake_embedder, fake_store)
    cfg = GroundingConfig(filter_fields=[])
    g = _grounder(fake_embedder, fake_store, fake_reranker, cfg)
    bundle = g.ground(query="sound vibration", grade=6, subject="Science")
    # with no filters, grade-7 content is reachable too
    ped_sources = {c.payload["source"] for c in bundle.chunks["pedagogical"]}
    assert "ped7" in ped_sources
