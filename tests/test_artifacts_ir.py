"""The standalone artifact IRs: guardrails, and byte-faithful projection from an
LDD (so the export-from-a-lesson path renders identically to before)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from lessonforge.domain.artifacts import Quiz, Slides, Worksheet


def test_quiz_from_ldd_carries_questions_objectives_and_sources(export_ldd):
    quiz = Quiz.from_ldd(export_ldd)
    assert [q.id for q in quiz.questions] == [q.id for q in export_ldd.formative_checks]
    assert {o.id for o in quiz.objectives} == {o.id for o in export_ldd.objectives}
    assert quiz.grounding_sources == export_ldd.quality.grounding_sources


def test_quiz_rejects_question_referencing_unknown_objective():
    with pytest.raises(ValidationError, match="unknown objectives"):
        Quiz(
            topic="T",
            curriculum_ref={"board": "CDC", "grade": 6, "subject": "Science"},
            objectives=[{"id": "O1", "statement": "know the thing", "bloom": "understand"}],
            questions=[{"id": "Q1", "type": "short_answer", "prompt": "why?",
                        "answer": "because", "objective_ids": ["O2"]}],
        )


def test_worksheet_needs_a_task_or_a_question():
    with pytest.raises(ValidationError, match="at least one task or question"):
        Worksheet(
            topic="T",
            curriculum_ref={"board": "CDC", "grade": 6, "subject": "Science"},
            objectives=[{"id": "O1", "statement": "know the thing", "bloom": "understand"}],
            tasks=[],
            questions=[],
        )


def test_worksheet_from_ldd_uses_homework_as_tasks(export_ldd):
    ws = Worksheet.from_ldd(export_ldd)
    assert ws.tasks == list(export_ldd.homework.instructions)
    assert [q.id for q in ws.questions] == [q.id for q in export_ldd.formative_checks]


def test_slides_from_ldd_is_hook_first(export_ldd):
    deck = Slides.from_ldd(export_ldd)
    assert deck.slides[0].heading == "Let's begin…"
    assert export_ldd.engagement_hook.prompt in deck.slides[0].bullets
    # one slide per phase + title-adjacent hook/objectives + a check slide
    assert deck.slides[1].heading == "What we'll be able to do"
    assert deck.slides[-1].heading == "Check for Understanding"


def test_coerce_passes_through_an_ir_and_projects_an_ldd(export_ldd):
    quiz = Quiz.from_ldd(export_ldd)
    assert Quiz.coerce(quiz) is quiz
    assert isinstance(Quiz.coerce(export_ldd), Quiz)
