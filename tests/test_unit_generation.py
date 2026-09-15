"""Multi-day units (Phase 4): plan → expand → coherence, and single-day
regeneration. A topic-aware fake LLM returns a unit plan for the planner prompt
and a per-day LDD for each generation prompt, so the whole arc is exercised
deterministically with no model."""

from __future__ import annotations

import copy
import json
import re

import pytest

from lessonforge.domain.unit import UnitRequest
from lessonforge.providers.base import LLMResult
from lessonforge.services.critique import StructuralCritic
from lessonforge.services.generation import LessonGenerator
from lessonforge.services.revise import Reviser
from lessonforge.services.unit_generation import UnitGenerator
from lessonforge.services.unit_planner import UnitPlanner
from tests.conftest import FakeLLM

_TOPIC_RE = re.compile(r"^Topic: (.+)$", re.MULTILINE)
_PRIOR_RE = re.compile(r"Earlier days already covered — (.+?)\. Do NOT", re.DOTALL)

_PLAN = {
    "title": "Scientific Study",
    "big_idea": "Science controls and measures variables",
    "unit_outcomes": ["Understand variables", "Understand graphs"],
    "thread": "a pulley lifting a load",
    "days": [
        {"day": 1, "topic": "Variables", "objective_seeds": ["Define variable"],
         "builds_on": "everyday observation", "sets_up": "classifying variables"},
        {"day": 2, "topic": "Types of Variables", "objective_seeds": ["Classify variables"],
         "builds_on": "the definition of a variable", "sets_up": "graphing"},
        {"day": 3, "topic": "Graphs and Data", "objective_seeds": ["Plot data"],
         "builds_on": "classified variables", "sets_up": "the unit assessment"},
    ],
}


def _lesson_for(topic: str, prior: str | None) -> dict:
    """A structurally valid LDD for one day, its content keyed to the topic so days
    differ (distinct topics + hooks) and prior knowledge echoes the earlier days."""
    prior_knowledge = [prior] if prior else ["Everyday experience from earlier grades"]
    return {
        "topic": topic,
        "curriculum_ref": {"board": "CDC", "grade": 10, "subject": "Science"},
        "duration_min": 45,
        "language": "en-ne",
        "framework": "5E",
        "objectives": [
            {"id": "O1", "statement": f"Understand and apply {topic}", "bloom": "understand"}
        ],
        "prior_knowledge": prior_knowledge,
        "misconceptions": [
            {"statement": f"{topic} is only theoretical",
             "correction": f"{topic} is used in real experiments"}
        ],
        "engagement_hook": {"prompt": f"Where do you notice {topic} in daily life?",
                            "kind": "question"},
        "local_context": ["paddy field", "river"],
        "phases": [
            {"name_en": "Explore", "name_ne": "अन्वेषण",
             "teacher_activities": [f"Demonstrate {topic}"],
             "student_activities": [f"Investigate {topic}"],
             "minutes": 45, "objective_ids": ["O1"]}
        ],
        "materials": ["Chart paper"],
        "formative_checks": [
            {"id": "Q1", "type": "short_answer", "prompt": f"What is {topic}?",
             "answer": topic, "objective_ids": ["O1"]}
        ],
        "homework": {"instructions": [f"Observe {topic} at home"], "objective_ids": ["O1"]},
    }


class UnitLLM(FakeLLM):
    """Plan for the spine prompt; a per-day LDD (keyed to the prompt's Topic) for a
    generation prompt. Records prompts so a test can assert the arc context."""

    def __init__(self) -> None:
        super().__init__(response={})
        self.calls: list[str] = []

    def complete(self, prompt, *, system=None, json_schema=None, temperature=None):
        self.calls.append(prompt)
        if "Design the spine of" in prompt:
            return LLMResult(text=json.dumps(_PLAN))
        topic_m = _TOPIC_RE.search(prompt)
        topic = topic_m.group(1).strip() if topic_m else "Untitled"
        prior_m = _PRIOR_RE.search(prompt)
        return LLMResult(text=json.dumps(_lesson_for(topic, prior_m.group(1) if prior_m else None)))


def _unit_generator(llm: UnitLLM) -> UnitGenerator:
    return UnitGenerator(
        planner=UnitPlanner(llm=llm),
        generator=LessonGenerator(llm=llm),
        reviser=Reviser(llm=None, critic=StructuralCritic()),  # passthrough
    )


def _req(num_days: int = 3) -> UnitRequest:
    return UnitRequest(topic="Scientific Study", grade=10, subject="Science", num_days=num_days)


# ── planner ───────────────────────────────────────────────────────────────────
def test_planner_produces_a_validated_spine():
    plan = UnitPlanner(llm=UnitLLM()).plan(_req())
    assert plan.title == "Scientific Study"
    assert [d.day for d in plan.days] == [1, 2, 3]  # renumbered gapless
    assert plan.curriculum_ref.grade == 10  # filled from the request
    assert plan.thread


def test_planner_renumbers_out_of_order_days():
    class MessyLLM(UnitLLM):
        def complete(self, prompt, **k):
            self.calls.append(prompt)
            messy = copy.deepcopy(_PLAN)
            messy["days"][0]["day"] = 7  # nonsense numbering
            messy["days"][2]["day"] = 2
            return LLMResult(text=json.dumps(messy))

    plan = UnitPlanner(llm=MessyLLM()).plan(_req())
    assert [d.day for d in plan.days] == [1, 2, 3]


def test_planner_rejects_a_plan_with_no_days():
    class EmptyLLM(FakeLLM):
        def complete(self, *a, **k):
            return LLMResult(text=json.dumps({"title": "X", "days": []}))

    with pytest.raises(ValueError, match="no days"):
        UnitPlanner(llm=EmptyLLM()).plan(_req())


# ── expansion ─────────────────────────────────────────────────────────────────
def test_generate_builds_one_lesson_per_planned_day():
    udd = _unit_generator(UnitLLM()).generate(_req())
    assert len(udd.days) == 3
    assert [d.topic for d in udd.days] == ["Variables", "Types of Variables", "Graphs and Data"]
    # every day is a fully valid LDD sharing the unit's grade/subject
    assert all(d.curriculum_ref.grade == 10 for d in udd.days)


def test_days_are_expanded_arc_aware():
    llm = UnitLLM()
    _unit_generator(llm).generate(_req())
    lesson_prompts = [p for p in llm.calls if p.startswith("Design a ")]
    # day 1 has no earlier days; day 2 sees day 1's real objective
    assert "This is Day 1" in lesson_prompts[0]
    assert "Earlier days already covered" not in lesson_prompts[0]
    assert "This is Day 2" in lesson_prompts[1]
    assert "Day 1 (Variables)" in lesson_prompts[1]
    assert "Understand and apply Variables" in lesson_prompts[1]  # the ACTUAL prior objective
    # the running example is carried into every day
    assert "pulley" in lesson_prompts[0]


def test_a_clean_unit_reports_coherent():
    udd = _unit_generator(UnitLLM()).generate(_req())
    assert udd.coherence.ok, [i.detail for i in udd.coherence.issues]


def test_regenerate_day_replaces_only_that_day():
    gen = _unit_generator(UnitLLM())
    udd = gen.generate(_req())
    original_ids = [id(d) for d in udd.days]
    updated = gen.regenerate_day(udd, 2, _req())
    # day 2 rebuilt; days 1 and 3 are byte-identical to before
    assert updated.days[0] == udd.days[0]
    assert updated.days[2] == udd.days[2]
    assert updated.days[1].topic == "Types of Variables"
    assert original_ids  # sanity


def test_regenerate_day_out_of_range_raises():
    gen = _unit_generator(UnitLLM())
    udd = gen.generate(_req())
    with pytest.raises(ValueError, match="out of range"):
        gen.regenerate_day(udd, 9, _req())
