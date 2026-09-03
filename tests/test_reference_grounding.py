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
