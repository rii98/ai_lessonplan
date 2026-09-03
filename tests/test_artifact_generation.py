"""Targeted-artifact generators: the generic seam produces a validated IR, stamps
real grounding sources, and degrades cleanly on unfixable output."""

from __future__ import annotations

import pytest

from lessonforge.domain.artifacts import Quiz, Slides, Worksheet
from lessonforge.domain.ldd import NormalizedBrief
from lessonforge.export import ArtifactKind
from lessonforge.rag.documents import Collection, Document
from lessonforge.rag.grounding import GroundingRetriever
from lessonforge.rag.ingest import Ingestor
from lessonforge.rag.retriever import Retriever
from lessonforge.services.artifact_generation import (
    GENERATOR_REGISTRY,
    build_generator,
    generatable_kinds,
)
from tests.conftest import FakeLLM

_BRIEF = NormalizedBrief(topic="Sound and Vibration", grade=7, subject="Science")

_VALID_QUIZ = {
    "topic": "Sound and Vibration",
    "curriculum_ref": {"board": "CDC", "grade": 7, "subject": "Science", "code": None},
    "objectives": [{"id": "O1", "statement": "Explain that sound is a vibration",
                    "bloom": "understand"}],
    "questions": [{"id": "Q1", "type": "short_answer", "prompt": "What causes sound?",
                   "answer": "vibration", "objective_ids": ["O1"], "options": None}],
    "instructions": "Answer all.",
}

_VALID_WORKSHEET = {
    "topic": "Sound and Vibration",
    "curriculum_ref": {"board": "CDC", "grade": 7, "subject": "Science", "code": None},
    "objectives": [{"id": "O1", "statement": "Explain that sound is a vibration",
                    "bloom": "understand"}],
    "tasks": ["Hum with a hand on your throat and note the buzz."],
    "questions": [{"id": "Q1", "type": "true_false", "prompt": "Sound needs vibration.",
                   "answer": "True", "objective_ids": ["O1"], "options": None}],
}

_VALID_SLIDES = {
    "topic": "Sound and Vibration",
    "curriculum_ref": {"board": "CDC", "grade": 7, "subject": "Science", "code": None},
    "subtitle": "CDC · Grade 7 · Science",
    "slides": [
        {"heading": "Let's begin…", "subtitle": "(demonstration)",
         "bullets": ["Hum with your hand on your throat."]},
        {"heading": "Check", "subtitle": None, "bullets": ["Name a vibrating object."]},
    ],
}


def test_all_three_artifacts_are_registered():
    assert set(generatable_kinds()) == {
        ArtifactKind.quiz, ArtifactKind.worksheet, ArtifactKind.slides
    }
    assert set(GENERATOR_REGISTRY) == set(generatable_kinds())


@pytest.mark.parametrize("kind,response,model", [
    (ArtifactKind.quiz, _VALID_QUIZ, Quiz),
    (ArtifactKind.worksheet, _VALID_WORKSHEET, Worksheet),
    (ArtifactKind.slides, _VALID_SLIDES, Slides),
])
def test_generator_produces_a_validated_ir(kind, response, model):
    gen = build_generator(kind, llm=FakeLLM(response))
    out = gen.generate(_BRIEF)
    assert isinstance(out, model)
    assert out.topic == "Sound and Vibration"


def test_grounding_sources_are_stamped_from_retrieval(fake_embedder, fake_store, fake_reranker):
    ing = Ingestor(embedder=fake_embedder, vector_store=fake_store)
    ing.ingest_documents(
        Collection.reference,
        [Document(id="r1", text="Sound is produced by vibration of objects.",
                  source="My Science Grade 7", metadata={"grade": 7, "subject": "Science"})],
    )
    retriever = Retriever(embedder=fake_embedder, vector_store=fake_store, reranker=fake_reranker)
    grounding = GroundingRetriever(retriever)
    gen = build_generator(ArtifactKind.quiz, llm=FakeLLM(_VALID_QUIZ), grounding=grounding)

    quiz = gen.generate(NormalizedBrief(topic="sound vibration", grade=7, subject="Science"))
    assert "My Science Grade 7" in quiz.grounding_sources


def test_unfixable_output_degrades_to_a_clean_error():
    # missing the required questions/objectives entirely → cannot assemble
    gen = build_generator(ArtifactKind.quiz, llm=FakeLLM({"topic": "x"}), max_repairs=1)
    with pytest.raises(ValueError, match="could not generate a valid quiz"):
        gen.generate(_BRIEF)
