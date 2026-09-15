"""The `reference` collection is preferred over general knowledge when it has
hits, and silently absent (degrade to model knowledge) when it doesn't."""

from __future__ import annotations

from lessonforge.config import GroundingConfig
from lessonforge.rag.documents import Collection, Document
from lessonforge.rag.grounding import GroundingBundle, GroundingRetriever
from lessonforge.rag.ingest import Ingestor
from lessonforge.rag.retriever import RetrievedChunk, Retriever


def test_bundle_puts_authoritative_first_under_prefer_directive():
    bundle = GroundingBundle(
        chunks={
            "pedagogical": [RetrievedChunk("teach it actively", 1.0, {"source": "ped"})],
            "reference": [RetrievedChunk("the environment has biotic parts", 1.0,
                                         {"source": "My Science Grade 6"})],
        },
        authoritative=frozenset({"reference"}),
    )
    ctx = bundle.as_prompt_context()
    assert bundle.has_authoritative
    # the AUTHORITATIVE block (reference) appears before the supporting block
    assert ctx.index("AUTHORITATIVE") < ctx.index("Supporting context")
    assert ctx.index("[reference]") < ctx.index("[pedagogical]")
    assert "prefer it over your own" in ctx


def test_empty_reference_degrades_to_model_knowledge():
    bundle = GroundingBundle(authoritative=frozenset({"reference"}))
    assert not bundle.has_authoritative
    assert "rely on curriculum knowledge" in bundle.as_prompt_context()


def test_retriever_marks_reference_authoritative_from_config(
    fake_embedder, fake_store, fake_reranker
):
    # seed only the reference collection so it produces the hits
    ing = Ingestor(embedder=fake_embedder, vector_store=fake_store)
    ing.ingest_documents(
        Collection.reference,
        [Document(id="r1", text="Biotic components are the living parts of the environment.",
                  source="My Science Grade 6", metadata={"grade": 6, "subject": "Science"})],
    )
    retriever = Retriever(embedder=fake_embedder, vector_store=fake_store, reranker=fake_reranker)
    gr = GroundingRetriever(retriever, GroundingConfig())  # reference is authoritative by default

    bundle = gr.ground(query="biotic components", grade=6, subject="Science")
    assert bundle.has_authoritative
    assert "My Science Grade 6" in bundle.sources
    assert "AUTHORITATIVE" in bundle.as_prompt_context()


# ── Phase 2 quality gate: recall-biased garbage floor + LLM verify ────────────
def _seed_reference(fake_embedder, fake_store):
    ing = Ingestor(embedder=fake_embedder, vector_store=fake_store)
    ing.ingest_documents(
        Collection.reference,
        [Document(id="r1", text="Biotic components are the living parts of the environment.",
                  source="My Science Grade 6", metadata={"grade": 6, "subject": "Science"})],
    )
    return Retriever(embedder=fake_embedder, vector_store=fake_store, reranker=fake_reranker_())


def fake_reranker_():
    from tests.conftest import FakeReranker

    return FakeReranker()


def test_floor_drops_a_collection_when_even_the_best_hit_is_weak(fake_embedder, fake_store):
    retriever = _seed_reference(fake_embedder, fake_store)
    # FakeReranker scores = keyword overlap count. A query sharing no words with the
    # doc scores 0 on the top hit; a high floor then drops it as "no usable context".
    cfg = GroundingConfig(min_score=1.0)
    gr = GroundingRetriever(retriever, cfg)
    bundle = gr.ground(query="xylophone trombone unrelated", grade=6, subject="Science")
    assert bundle.chunks["reference"] == []  # gated out
    assert not bundle.has_authoritative
    assert "rely on curriculum knowledge" in bundle.as_prompt_context()


def test_floor_keeps_a_collection_when_the_top_hit_clears_it(fake_embedder, fake_store):
    retriever = _seed_reference(fake_embedder, fake_store)
    cfg = GroundingConfig(min_score=1.0)
    gr = GroundingRetriever(retriever, cfg)
    # a query overlapping the doc's words → top score >= 1 → kept
    bundle = gr.ground(query="biotic living parts", grade=6, subject="Science")
    assert bundle.chunks["reference"]
    assert bundle.has_authoritative


def test_no_floor_preserves_current_behaviour(fake_embedder, fake_store):
    retriever = _seed_reference(fake_embedder, fake_store)
    gr = GroundingRetriever(retriever, GroundingConfig())  # min_score None (default)
    bundle = gr.ground(query="anything at all", grade=6, subject="Science")
    assert bundle.chunks["reference"]  # nothing gated when the floor is off


def test_llm_verify_drops_off_topic_authoritative_hits(fake_embedder, fake_store):
    from lessonforge.providers.base import LLMResult
    from tests.conftest import FakeLLM

    retriever = _seed_reference(fake_embedder, fake_store)
    verifier = FakeLLM()
    verifier.complete = lambda *a, **k: LLMResult(text="no")  # type: ignore[method-assign]
    gr = GroundingRetriever(retriever, GroundingConfig(verify=True), llm=verifier)
    bundle = gr.ground(query="biotic living parts", grade=6, subject="Science")
    assert bundle.chunks["reference"] == []  # judge said off-topic → dropped


def test_llm_verify_is_fail_open(fake_embedder, fake_store):
    from tests.conftest import FakeLLM

    retriever = _seed_reference(fake_embedder, fake_store)

    class BoomLLM(FakeLLM):
        def complete(self, *a, **k):
            raise RuntimeError("model down")

    gr = GroundingRetriever(retriever, GroundingConfig(verify=True), llm=BoomLLM())
    bundle = gr.ground(query="biotic living parts", grade=6, subject="Science")
    assert bundle.chunks["reference"]  # verification failure keeps the chunks
