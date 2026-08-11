"""Enrichment is wired to real retrieval: the grounding context reaches the
prompt, and the retrieved sources — not the model's claims — become the LDD's
provenance."""

from __future__ import annotations

from lessonforge.config import GroundingConfig
from lessonforge.domain.ldd import NormalizedBrief
from lessonforge.rag.documents import Collection, Document
from lessonforge.rag.grounding import GroundingRetriever
from lessonforge.rag.ingest import Ingestor
from lessonforge.rag.retriever import Retriever
from lessonforge.services.generation import LessonGenerator
from tests.conftest import FakeLLM


def _grounding(fake_embedder, fake_store, fake_reranker) -> GroundingRetriever:
    Ingestor(embedder=fake_embedder, vector_store=fake_store).ingest_documents(
        Collection.pedagogical,
        [Document(id="p1", text="clouds move but are not living", source="Seed pedagogy",
                  metadata={"grade": 6, "subject": "Science"})],
    )
    retriever = Retriever(embedder=fake_embedder, vector_store=fake_store,
                          reranker=fake_reranker, top_k=20, rerank_top_n=3)
    return GroundingRetriever(retriever, GroundingConfig())


def test_grounding_context_reaches_the_prompt(
    fake_embedder, fake_store, fake_reranker, valid_ldd_dict
):
    llm = FakeLLM(response=valid_ldd_dict)
    gen = LessonGenerator(llm=llm, grounding=_grounding(fake_embedder, fake_store, fake_reranker))
    gen.generate(NormalizedBrief(topic="Environment", grade=6, subject="Science"))
    prompt = llm.calls[0]["prompt"]
    assert "Grounding context" in prompt
    assert "clouds move but are not living" in prompt


def test_retrieved_sources_override_model_provenance(
    fake_embedder, fake_store, fake_reranker, valid_ldd_dict
):
    # the model claims a bogus source; the real retrieved source must win
    dishonest = dict(valid_ldd_dict)
    dishonest["quality"] = {"grounding_sources": ["Made-up citation the model invented"]}
    llm = FakeLLM(response=dishonest)
    gen = LessonGenerator(llm=llm, grounding=_grounding(fake_embedder, fake_store, fake_reranker))
    ldd = gen.generate(NormalizedBrief(topic="Environment", grade=6, subject="Science"))
    assert ldd.quality.grounding_sources == ["Seed pedagogy"]
    assert "Made-up citation the model invented" not in ldd.quality.grounding_sources


def test_generation_without_grounding_still_works(valid_ldd_dict):
    gen = LessonGenerator(llm=FakeLLM(response=valid_ldd_dict), grounding=None)
    ldd = gen.generate(NormalizedBrief(topic="Environment", grade=6, subject="Science"))
    assert ldd.quality.grounding_sources == []  # nothing retrieved, nothing claimed
