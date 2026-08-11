"""The intake stage — heuristic and LLM parsers, and US-3 paste-a-plan."""

from __future__ import annotations

import pytest

from lessonforge.config import IntakeConfig
from lessonforge.domain.ldd import IntakeRequest
from lessonforge.providers.base import LLMResult
from lessonforge.services.intake import HeuristicIntake, LLMIntake
from lessonforge.services.registry import build_intake
from tests.conftest import FakeLLM

_PASTED = """\
Class 7 Science — 40 minutes
Topic: Photosynthesis in Green Plants
We will read the textbook and copy the diagram of a leaf.
"""


def test_heuristic_extracts_fields_from_pasted_plan():
    brief = HeuristicIntake().normalize(IntakeRequest(existing_plan=_PASTED))
    assert brief.topic == "Photosynthesis in Green Plants"
    assert brief.grade == 7
    assert brief.subject == "Science"
    assert brief.duration_min == 45  # 40 snaps to the nearest valid duration
    assert brief.existing_plan is not None  # preserved for enrich-not-replace


def test_explicit_fields_win_over_parsed():
    brief = HeuristicIntake().normalize(
        IntakeRequest(existing_plan=_PASTED, grade=9, subject="Mathematics")
    )
    assert brief.grade == 9
    assert brief.subject == "Mathematics"


def test_heuristic_uses_first_line_as_topic_when_unlabelled():
    brief = HeuristicIntake().normalize(
        IntakeRequest(existing_plan="Volcanoes and Plates\nGrade 8 Science lesson", grade=8)
    )
    assert brief.topic == "Volcanoes and Plates"


def test_missing_topic_raises():
    with pytest.raises(ValueError, match="topic"):
        HeuristicIntake().normalize(IntakeRequest(grade=6))


def test_missing_grade_raises():
    with pytest.raises(ValueError, match="grade"):
        HeuristicIntake().normalize(IntakeRequest(topic="Anything"))


def test_no_plan_builds_brief_from_explicit_fields():
    brief = HeuristicIntake().normalize(
        IntakeRequest(topic="Fractions", grade=6, subject="Mathematics")
    )
    assert brief.topic == "Fractions" and brief.grade == 6
    assert brief.duration_min == 45 and brief.framework == "5E"  # defaults


def test_llm_intake_uses_model_extraction():
    llm = FakeLLM(response={"topic": "Cell Division", "grade": 8, "subject": "Science"})
    brief = LLMIntake(llm=llm).normalize(IntakeRequest(existing_plan="some messy notes"))
    assert brief.topic == "Cell Division" and brief.grade == 8


def test_llm_intake_falls_back_to_heuristic_on_bad_json():
    class BadLLM(FakeLLM):
        def complete(self, *a, **k):
            return LLMResult(text="not json")

    brief = LLMIntake(llm=BadLLM()).normalize(IntakeRequest(existing_plan=_PASTED))
    assert brief.topic == "Photosynthesis in Green Plants" and brief.grade == 7


def test_llm_intake_skips_model_when_no_plan():
    llm = FakeLLM(response={"topic": "should-not-be-used"})
    brief = LLMIntake(llm=llm).normalize(IntakeRequest(topic="Real Topic", grade=6))
    assert brief.topic == "Real Topic"
    assert llm.calls == []  # no pasted plan → no model call


def test_build_intake_from_registry():
    intake = build_intake(IntakeConfig(provider="heuristic"), llm=FakeLLM())
    assert isinstance(intake, HeuristicIntake)


def test_build_intake_unknown_provider_fails_loudly():
    with pytest.raises(ValueError, match="Unknown intake provider"):
        build_intake(IntakeConfig(provider="nope"), llm=FakeLLM())
