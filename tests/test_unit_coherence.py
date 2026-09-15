"""UnitCoherence (Phase 4): the deterministic cross-day arc check. Built from
hand-assembled units so each heuristic is isolated."""

from __future__ import annotations

import copy

import pytest

from lessonforge.domain.ldd import LessonDesignDocument
from lessonforge.domain.unit import DayPlan, UnitDesignDocument, UnitPlan
from lessonforge.services.unit_coherence import UnitCoherence


def _day(topic: str, *, hook="A curious question about the world?", minutes=45,
         objective="Understand the core idea", prior=("earlier work",)) -> LessonDesignDocument:
    return LessonDesignDocument.model_validate({
        "topic": topic,
        "curriculum_ref": {"board": "CDC", "grade": 10, "subject": "Science"},
        "duration_min": 45,
        "language": "en-ne",
        "framework": "5E",
        "objectives": [{"id": "O1", "statement": objective, "bloom": "understand"}],
        "prior_knowledge": list(prior),
        "engagement_hook": {"prompt": hook, "kind": "question"},
        "phases": [{"name_en": "Explore", "teacher_activities": ["t"],
                    "student_activities": ["s"], "minutes": minutes, "objective_ids": ["O1"]}],
        "formative_checks": [{"id": "Q1", "type": "short_answer", "prompt": "p",
                              "answer": "a", "objective_ids": ["O1"]}],
    })


def _unit(days, *, outcomes=(), plan_days=None) -> UnitDesignDocument:
    plan = UnitPlan(
        title="Unit", curriculum_ref={"grade": 10, "subject": "Science"},
        unit_outcomes=list(outcomes),
        days=[DayPlan(day=i + 1, topic=d.topic) for i, d in enumerate(plan_days or days)],
    )
    return UnitDesignDocument(
        title="Unit", curriculum_ref={"grade": 10, "subject": "Science"},
        unit_outcomes=list(outcomes), plan=plan, days=days,
    )


def _kinds(report):
    return {i.kind for i in report.issues}


def test_clean_unit_is_coherent():
    days = [
        _day("Variables", objective="Understand variables in experiments"),
        _day("Graphs", hook="How does a rising line tell a story?",
             objective="Interpret graphs of variables", prior=("variables",)),
    ]
    report = UnitCoherence().assess(_unit(days, outcomes=["Understand variables graphs"]))
    assert report.ok and report.issues == []


def test_duplicate_topic_is_flagged():
    days = [_day("Variables"), _day("Variables", hook="A different hook entirely?")]
    report = UnitCoherence().assess(_unit(days))
    assert "duplicate_topic" in _kinds(report)


def test_repeated_hook_is_flagged():
    days = [_day("Variables"), _day("Graphs")]  # same default hook
    report = UnitCoherence().assess(_unit(days))
    assert "repeated_hook" in _kinds(report)


def test_minute_budget_is_flagged():
    days = [_day("Variables", minutes=5, hook="One?"), _day("Graphs", minutes=45, hook="Two?")]
    report = UnitCoherence().assess(_unit(days))
    issues = [i for i in report.issues if i.kind == "minute_budget"]
    assert issues and issues[0].day == 1  # the 5-min day, not the 45-min one


def test_outcome_gap_is_flagged():
    days = [_day("Variables", hook="One?", objective="Understand variables in experiments")]
    report = UnitCoherence().assess(_unit(days, outcomes=["Master photosynthesis chemistry"]))
    assert "outcome_gap" in _kinds(report)


def test_continuity_gap_is_flagged():
    days = [
        _day("Variables", hook="One?", objective="Understand variables in experiments"),
        _day("Graphs", hook="Two?", objective="Interpret graphs of data",
             prior=("completely unrelated astronomy",)),
    ]
    report = UnitCoherence().assess(_unit(days))
    assert "continuity" in _kinds(report)


def test_apply_attaches_report_to_the_unit():
    days = [_day("Variables"), _day("Variables", hook="dupe?")]
    udd = UnitCoherence().apply(_unit(days))
    assert not udd.coherence.ok
    assert "duplicate_topic" in {i.kind for i in udd.coherence.issues}


# ── UnitDesignDocument structural validators ──────────────────────────────────
def test_unit_rejects_day_count_mismatch():
    days = [_day("A"), _day("B", hook="b?")]
    plan = UnitPlan(title="U", curriculum_ref={"grade": 10, "subject": "Science"},
                    days=[DayPlan(day=1, topic="A")])  # only 1 planned
    with pytest.raises(ValueError, match="lays out"):
        UnitDesignDocument(title="U", curriculum_ref={"grade": 10, "subject": "Science"},
                           plan=plan, days=days)


def test_unit_rejects_grade_mismatch_across_days():
    good = _day("A")
    other = copy.deepcopy(good.model_dump())
    other["curriculum_ref"]["grade"] = 9
    mismatched = LessonDesignDocument.model_validate(other)
    plan = UnitPlan(title="U", curriculum_ref={"grade": 10, "subject": "Science"},
                    days=[DayPlan(day=1, topic="A")])
    with pytest.raises(ValueError, match="not the unit's"):
        UnitDesignDocument(title="U", curriculum_ref={"grade": 10, "subject": "Science"},
                           plan=plan, days=[mismatched])


def test_unit_plan_rejects_nonsequential_days():
    with pytest.raises(ValueError, match="1..N"):
        UnitPlan(title="U", curriculum_ref={"grade": 10, "subject": "Science"},
                 days=[DayPlan(day=1, topic="A"), DayPlan(day=3, topic="B")])
