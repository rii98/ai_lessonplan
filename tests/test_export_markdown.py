"""Markdown renderers: deterministic bytes → golden-file tests, plus content
guarantees (timing surfaced, question types, answer key present)."""

from __future__ import annotations

from pathlib import Path

import pytest

from lessonforge.domain.ldd import LessonDesignDocument
from lessonforge.export import ArtifactKind, build_renderer

_GOLDEN = Path(__file__).parent / "golden"


@pytest.mark.parametrize("kind", [ArtifactKind.lesson_plan, ArtifactKind.worksheet, ArtifactKind.quiz])
def test_markdown_matches_golden_bytes(export_ldd, kind):
    """Fixed LDD → stable bytes. Regenerate golden files intentionally if the
    layout changes; an accidental drift fails here."""
    art = build_renderer(kind, "md").render(export_ldd)
    golden = (_GOLDEN / f"{kind.value}.md").read_bytes()
    assert art.content == golden, f"{kind.value}.md drifted from golden"


def test_markdown_is_deterministic(export_ldd):
    a = build_renderer(ArtifactKind.lesson_plan, "md").render(export_ldd)
    b = build_renderer(ArtifactKind.lesson_plan, "md").render(export_ldd)
    assert a.content == b.content


def test_lesson_plan_surfaces_timing(export_ldd):
    md = build_renderer(ArtifactKind.lesson_plan, "md").render(export_ldd).content.decode()
    assert "planned 45 min / target 45 min" in md
    assert "balanced" in md


def test_timing_flags_overrun():
    # phases sum to 60 but target is 45 → flagged as over by 15
    data = {
        "topic": "Timing test",
        "curriculum_ref": {"board": "CDC", "grade": 6, "subject": "Science"},
        "duration_min": 45, "framework": "5E",
        "objectives": [{"id": "O1", "statement": "do the thing well", "bloom": "apply"}],
        "engagement_hook": {"prompt": "Why does this happen?", "kind": "question"},
        "phases": [{"name_en": "Explore", "teacher_activities": ["t"],
                    "student_activities": ["s"], "minutes": 60, "objective_ids": ["O1"]}],
        "formative_checks": [{"id": "Q1", "type": "short_answer", "prompt": "why?",
                              "answer": "because", "objective_ids": ["O1"]}],
    }
    ldd = LessonDesignDocument.model_validate(data)
    md = build_renderer(ArtifactKind.lesson_plan, "md").render(ldd).content.decode()
    assert "over by 15 min" in md


def test_worksheet_renders_all_question_types_and_answer_key(export_ldd):
    md = build_renderer(ArtifactKind.worksheet, "md").render(export_ldd).content.decode()
    assert "A. soil" in md and "B. goat" in md          # mcq options lettered
    assert "( ) True    ( ) False" in md                # true/false
    assert "Answer: _" in md                            # short answer blank
    key = md.split("## Answer Key", 1)[1]
    assert "goat" in key and "True" in key and "rice plant" in key


def test_quiz_has_score_line_and_answer_key(export_ldd):
    md = build_renderer(ArtifactKind.quiz, "md").render(export_ldd).content.decode()
    assert "**Score:** _____ / 3" in md
    assert "## Answer Key" in md


def test_filename_is_ascii_slug_even_for_devanagari_topic():
    data = {
        "topic": "वातावरण",  # pure Devanagari topic
        "curriculum_ref": {"board": "CDC", "grade": 6, "subject": "Science"},
        "duration_min": 45, "framework": "5E",
        "objectives": [{"id": "O1", "statement": "understand the environment", "bloom": "understand"}],
        "engagement_hook": {"prompt": "What surrounds you?", "kind": "question"},
        "phases": [{"name_en": "Engage", "teacher_activities": ["t"],
                    "student_activities": ["s"], "minutes": 45, "objective_ids": ["O1"]}],
        "formative_checks": [{"id": "Q1", "type": "short_answer", "prompt": "what?",
                              "answer": "air", "objective_ids": ["O1"]}],
    }
    ldd = LessonDesignDocument.model_validate(data)
    art = build_renderer(ArtifactKind.lesson_plan, "md").render(ldd)
    assert art.filename.isascii()
    assert art.filename == "lesson_lesson_plan.md"  # slug falls back when no ASCII
    # but the CONTENT keeps the Devanagari topic
    assert "वातावरण" in art.content.decode()
