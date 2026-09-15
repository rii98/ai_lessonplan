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
import logging
from typing import Any

from ..domain.ldd import CurriculumRef
from ..domain.profile import TeacherProfile
from ..domain.unit import CoverageReport, UnitPlan, UnitRequest
from ..providers.base import LLMClient
from ..rag.grounding import (
    CoverageOutline,
    GroundingBundle,
    GroundingRetriever,
    coverage_gaps,
)
from ..rag.planner import RetrievalPlanner
from ..util import extract_json

_log = logging.getLogger(__name__)

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
{coverage}
{grounding}

Return a single JSON object with EXACTLY the same keys and nesting as this example
(replace the content, keep the structure), but with EXACTLY {num_days} entries in
"days", numbered 1..{num_days} in teaching order:

{example}

Hard requirements:
- If a SYLLABUS is given above, every one of its sections MUST appear in some
  day's topic or objective_seeds — completeness is mandatory. Group adjacent
  sections into a day when there are more sections than days; never drop one.
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
        """The spine only — used by the expander, which doesn't need the coverage
        report. UI callers want :meth:`plan_with_coverage`."""
        plan, _ = self.plan_with_coverage(req, profile=profile)
        return plan

    def plan_with_coverage(
        self, req: UnitRequest, *, profile: TeacherProfile | None = None
    ) -> tuple[UnitPlan, CoverageReport]:
        """The spine plus how completely it covers the chapter's syllabus, so the
        teacher sees at a glance whether any topic was dropped before expanding."""
        bundle = self._ground(req)
        outline = self._outline(req)
        prompt = _PROMPT_TEMPLATE.format(
            num_days=req.num_days,
            grade=req.grade,
            subject=req.subject,
            topic=req.topic,
            coverage=outline.as_prompt_context(),
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
        plan = self._assemble(data, req)
        return plan, self._coverage_report(outline, plan)

    def _coverage_report(self, outline: CoverageOutline, plan: UnitPlan) -> CoverageReport:
        """Build the coverage report and log any gap. Missing content is the failure
        mode this whole path exists to prevent, so a gap is WARN-logged (naming each
        uncovered topic) as well as returned — visible whether or not a UI renders
        it, without failing the build on brittle text matching."""
        if outline.is_empty:
            return CoverageReport()
        gaps = coverage_gaps(outline.sections, plan)
        if gaps:
            _log.warning(
                "unit plan for %r may not cover %d/%d syllabus topic(s): %s",
                plan.title, len(gaps), len(outline.sections), "; ".join(gaps),
            )
        return CoverageReport(
            source=", ".join(dict.fromkeys(outline.chapters)),
            topics=list(outline.sections),
            gaps=gaps,
        )

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

    def _outline(self, req: UnitRequest) -> CoverageOutline:
        """The complete section list the plan must cover, enumerated from the book's
        heading hierarchy (not vector similarity) so no topic is dropped. Failures
        and books with no hierarchy degrade to an empty outline — the plan is still
        produced, just without the coverage contract."""
        if self.grounding is None:
            return CoverageOutline()
        try:
            return self.grounding.outline(
                query=f"{req.topic} grade {req.grade} {req.subject}",
                grade=req.grade,
                subject=req.subject,
            )
        except Exception:
            return CoverageOutline()
