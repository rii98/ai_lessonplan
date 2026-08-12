"""The end-to-end pipeline composed from fakes: intake → enrich → critique/revise."""

from __future__ import annotations

from lessonforge.domain.ldd import IntakeRequest
from lessonforge.services.critique import StructuralCritic
from lessonforge.services.generation import LessonGenerator
from lessonforge.services.intake import HeuristicIntake
from lessonforge.services.pipeline import LessonPipeline
from lessonforge.services.revise import Reviser
from tests.conftest import FakeLLM


def _pipeline(response: dict) -> LessonPipeline:
    return LessonPipeline(
        intake=HeuristicIntake(),
        generator=LessonGenerator(llm=FakeLLM(response=response), grounding=None),
        reviser=Reviser(llm=None, critic=StructuralCritic(), threshold=0.7, max_iterations=0),
    )


def test_pipeline_runs_end_to_end(valid_ldd_dict):
    out = _pipeline(valid_ldd_dict).run(
        IntakeRequest(topic="Environment", grade=6, subject="Science")
    )
    assert out.objectives[0].id == "O1"
    assert out.quality.notes  # critique stamped scores into quality


def test_pipeline_parses_pasted_plan(valid_ldd_dict):
    # US-3: no explicit topic/grade — intake pulls them from the pasted plan
    out = _pipeline(valid_ldd_dict).run(
        IntakeRequest(existing_plan="Grade 6 Science\nTopic: Environment\nread the book")
    )
    assert out.objectives[0].id == "O1"


def test_pipeline_critique_delegates(export_ldd, valid_ldd_dict):
    critique = _pipeline(valid_ldd_dict).critique(export_ldd)
    assert 0.0 <= critique.overall <= 1.0
    assert critique.scores.local_relevance > 0.0


def test_pipeline_preserves_adjustment_trail(valid_ldd_dict):
    # a normalization applied at generation must survive the critique/revise stamp
    import copy
    bad = copy.deepcopy(valid_ldd_dict)
    bad["engagement_hook"]["kind"] = "analogy"
    out = _pipeline(bad).run(IntakeRequest(topic="Environment", grade=6, subject="Science"))
    assert out.engagement_hook.kind == "scenario"
    assert any("analogy" in a for a in out.quality.adjustments)  # trail intact
    assert out.quality.notes  # critique still stamped its own notes separately
