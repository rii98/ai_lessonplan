"""UnitPlanner — the first half of plan-and-expand: brief → validated UnitPlan.

One cheap LLM call produces the unit *spine* (the arc), grounded in a BROAD view
of the reference book (the whole chapter, via multi-granularity retrieval) so the
plan reflects the real curriculum sequence rather than the model's guess. The
spine is small and validated, so it can be shown to the teacher and regenerated
freely *before* the expensive per-day expansion runs.

Kept separate from the expander (:mod:`unit_generation`) so the plan is a
first-class, reviewable artifact — the gate in front of the costly step.
"""

from __future__ import annotations

import json
from typing import Any

from ..domain.ldd import CurriculumRef
from ..domain.profile import TeacherProfile
from ..domain.unit import UnitPlan, UnitRequest
from ..providers.base import LLMClient
from ..rag.grounding import GroundingBundle, GroundingRetriever
from ..rag.planner import RetrievalPlanner
from ..util import extract_json

_SYSTEM = (
    "You are an experienced Nepali secondary-school curriculum designer laying out "
    "a multi-day teaching unit. You sequence topics so each day builds on the last "
    "and sets up the next, and you carry ONE running example through the unit to "
    "connect the days. You output ONLY valid JSON matching the provided schema."
)

# Shape anchor (two days); the model replaces the content and produces exactly the
# requested number of days. Cloud models that ignore Ollama's `format` still follow
# a shown example, so this is what makes the structured output reliable.
_EXAMPLE: dict[str, Any] = {
    "title": "Scientific Study",
    "curriculum_ref": {"board": "CDC", "grade": 10, "subject": "Science", "code": None},
    "big_idea": "Science answers questions by controlling and measuring variables.",
    "unit_outcomes": [
        "Distinguish independent, dependent, and controlled variables",
        "Represent and interpret a relationship between two variables",
    ],
    "thread": "a pulley lifting a load",
    "days": [
        {"day": 1, "topic": "Introduction to Scientific Study and Variables",
         "objective_seeds": ["Define variable", "List the steps of scientific study"],
         "builds_on": "everyday observation from earlier grades",
         "sets_up": "classifying variables on day 2"},
        {"day": 2, "topic": "Independent, Dependent, and Controlled Variables",
         "objective_seeds": ["Differentiate the three kinds of variable"],
         "builds_on": "the definition of a variable from day 1",
         "sets_up": "graphing the relationship on day 3"},
    ],
}

_PROMPT_TEMPLATE = """\
Design the spine of a {num_days}-day unit for Grade {grade} {subject}.
Unit topic: {topic}
{grounding}

Return a single JSON object with EXACTLY the same keys and nesting as this example
(replace the content, keep the structure), but with EXACTLY {num_days} entries in
"days", numbered 1..{num_days} in teaching order:

{example}

Hard requirements:
- Exactly {num_days} days, numbered 1..{num_days}, each a distinct topic that
  progresses logically (no repeats, no gaps).
- Each day states how it builds_on the previous day and sets_up the next.
- Carry ONE running example across the unit in "thread".
- unit_outcomes are the whole-unit learning outcomes (2-5 of them).
Return JSON only, no prose, no markdown fences.
"""


class UnitPlanner:
    def __init__(
        self,
        *,
        llm: LLMClient,
        grounding: GroundingRetriever | None = None,
        retrieval_planner: RetrievalPlanner | None = None,
    ) -> None:
        self.llm = llm
        self.grounding = grounding
        self.retrieval_planner = retrieval_planner

    def plan(self, req: UnitRequest, *, profile: TeacherProfile | None = None) -> UnitPlan:
        bundle = self._ground(req)
        prompt = _PROMPT_TEMPLATE.format(
            num_days=req.num_days,
            grade=req.grade,
            subject=req.subject,
            topic=req.topic,
            grounding=bundle.as_prompt_context(),
            example=json.dumps(_EXAMPLE, ensure_ascii=False, indent=2),
        )
        try:
            result = self.llm.complete(
                prompt, system=_SYSTEM, json_schema=UnitPlan.model_json_schema()
            )
            data = extract_json(result.text)
        except Exception as exc:
            raise ValueError(f"could not plan the unit: {exc}") from exc
        return self._assemble(data, req)

    def _assemble(self, data: Any, req: UnitRequest) -> UnitPlan:
        """Normalize the model's draft into a valid UnitPlan: renumber days into a
        gapless 1..N sequence (models often mis-number), and fill the curriculum
        ref from the request so it always matches what was asked for."""
        if not isinstance(data, dict):
            # ValueError (not TypeError): all planner failures surface as ValueError
            # so callers catch one exception type for "the model gave us junk".
            raise ValueError("unit plan must be a JSON object")  # noqa: TRY004
        data.setdefault("title", req.topic)
        data["curriculum_ref"] = CurriculumRef(
            grade=req.grade, subject=req.subject
        ).model_dump()
        days = data.get("days")
        if not isinstance(days, list) or not days:
            raise ValueError("unit plan has no days")
        for i, day in enumerate(days, 1):
            if isinstance(day, dict):
                day["day"] = i  # gapless, in given order → satisfies the validator
        try:
            return UnitPlan.model_validate(data)
        except Exception as exc:
            raise ValueError(f"the model's unit plan was invalid: {exc}") from exc

    def _ground(self, req: UnitRequest) -> GroundingBundle:
        """Retrieve BROAD reference context for the whole unit — the chapter, not a
        chunk. The retrieval planner confirms the granularity (a unit plan is a
        broad need); grounding failures degrade to an empty bundle, never crash."""
        if self.grounding is None:
            return GroundingBundle()
        granularity = "broad"
        if self.retrieval_planner is not None:
            granularity = self.retrieval_planner.plan(req.topic, kind="unit_plan").granularity
        try:
            return self.grounding.ground(
                query=f"{req.topic} grade {req.grade} {req.subject}",
                grade=req.grade,
                subject=req.subject,
                framework=req.framework,
                granularity=granularity,  # type: ignore[arg-type]
            )
        except Exception:
            return GroundingBundle()
