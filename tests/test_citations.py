"""Mandatory citations: every generated document carries sources (real, or the
honest 'model general knowledge' marker), and every renderer prints them."""

from __future__ import annotations

import io
import zipfile

import pytest

from lessonforge.domain.artifacts import Quiz
from lessonforge.export import ArtifactKind, build_renderer
from lessonforge.rag.grounding import (
    NO_SOURCE_MARKER,
    ensure_sources,
    merge_sources,
)


def test_ensure_sources_falls_back_to_marker_and_dedupes():
    assert ensure_sources([]) == [NO_SOURCE_MARKER]
    assert ensure_sources(["A", "A", " B "]) == ["A", "B"]


def test_merge_sources_grows_and_drops_the_marker():
    assert merge_sources([NO_SOURCE_MARKER], ["Book A"]) == ["Book A"]
    assert merge_sources(["Book A"], ["Book A", "Book B"]) == ["Book A", "Book B"]
    # nothing real to add → keep what we had
    assert merge_sources(["Book A"], [NO_SOURCE_MARKER]) == ["Book A"]


def test_ungrounded_generate_stamps_the_marker(fake_embedder, fake_store, fake_reranker):
    from lessonforge.domain.ldd import NormalizedBrief
    from lessonforge.rag.grounding import GroundingRetriever
    from lessonforge.rag.retriever import Retriever
    from lessonforge.services.artifact_generation import build_generator
    from tests.conftest import FakeLLM

    quiz_json = {
        "topic": "Sound", "curriculum_ref": {"board": "CDC", "grade": 7, "subject": "Science"},
        "objectives": [{"id": "O1", "statement": "explain sound is vibration", "bloom": "understand"}],
        "questions": [{"id": "Q1", "type": "short_answer", "prompt": "cause?",
                       "answer": "vibration", "objective_ids": ["O1"]}],
    }
    grounding = GroundingRetriever(  # empty store → no hits
        Retriever(embedder=fake_embedder, vector_store=fake_store, reranker=fake_reranker)
    )
    gen = build_generator(ArtifactKind.quiz, llm=FakeLLM(quiz_json), grounding=grounding)
    quiz = gen.generate(NormalizedBrief(topic="Sound", grade=7, subject="Science"))
    assert quiz.grounding_sources == [NO_SOURCE_MARKER]


@pytest.mark.parametrize("kind,fmt", [
    (ArtifactKind.lesson_plan, "md"), (ArtifactKind.lesson_plan, "docx"),
    (ArtifactKind.quiz, "md"), (ArtifactKind.quiz, "docx"),
    (ArtifactKind.worksheet, "md"), (ArtifactKind.worksheet, "docx"),
    (ArtifactKind.slides, "pptx"),
])
def test_every_renderer_prints_a_sources_section(export_ldd, kind, fmt):
    art = build_renderer(kind, fmt).render(export_ldd)
    if fmt == "md":
        assert "## Sources" in art.content.decode()
    else:
        # docx/pptx are zips — the text lives in the document XML members
        xml = _office_text(art.content)
        assert "Sources" in xml and "CDC Science Grade 6" in xml


def _office_text(content: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        return "".join(
            zf.read(n).decode("utf-8", "ignore")
            for n in zf.namelist()
            if n.endswith(".xml")
        )


def test_hand_built_artifact_without_sources_still_renders_the_marker():
    quiz = Quiz.model_validate({
        "topic": "Sound", "curriculum_ref": {"board": "CDC", "grade": 7, "subject": "Science"},
        "objectives": [{"id": "O1", "statement": "explain sound is vibration", "bloom": "understand"}],
        "questions": [{"id": "Q1", "type": "short_answer", "prompt": "cause?",
                       "answer": "vibration", "objective_ids": ["O1"]}],
    })  # no grounding_sources
    md = build_renderer(ArtifactKind.quiz, "md").render(quiz).content.decode()
    assert NO_SOURCE_MARKER in md
