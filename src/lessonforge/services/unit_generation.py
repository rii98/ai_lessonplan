"""UnitGenerator — the second half of plan-and-expand: UnitPlan → UnitDesignDocument.

Each day of the spine is expanded into a full LDD by the *existing* generator and
critique/revise loop — so every per-day guardrail and renderer is reused, not
re-implemented. Expansion is sequential by design: each day is generated with the
*actual* objectives of the days already built, so the arc is real (Day 3 knows
what Day 2 concluded), not merely promised by the plan. A final deterministic
coherence pass reports any cross-day problems.

Regenerating a single day (:meth:`regenerate_day`) rebuilds just that day against
the same arc context and re-runs coherence — so "redo Day 3" leaves 1/2/4/5 alone,
which is the whole reason the unit is a composition of independent LDDs.
"""

from __future__ import annotations

from ..domain.ldd import LessonDesignDocument, NormalizedBrief
from ..domain.profile import TeacherProfile
from ..domain.unit import UnitDesignDocument, UnitPlan, UnitRequest
from .generation import LessonGenerator
from .revise import Reviser
from .unit_coherence import UnitCoherence
from .unit_planner import UnitPlanner

# NormalizedBrief constrains these; a UnitRequest carries plain ints/strs, so we
# coerce to the nearest valid value rather than letting an odd number reject.
_VALID_DURATIONS = (30, 45, 60)


def _coerce_duration(minutes: int) -> int:
    return min(_VALID_DURATIONS, key=lambda v: abs(v - minutes))


class UnitGenerator:
    def __init__(
        self,
        *,
        planner: UnitPlanner,
        generator: LessonGenerator,
        reviser: Reviser,
        coherence: UnitCoherence | None = None,
    ) -> None:
        self.planner = planner
        self.generator = generator
        self.reviser = reviser
        self.coherence = coherence or UnitCoherence()

    # ── full pipeline ─────────────────────────────────────────────────────────
    def generate(
        self, req: UnitRequest, *, profile: TeacherProfile | None = None
    ) -> UnitDesignDocument:
        """Plan the spine, then expand every day. The plan gates the expensive
        expansion — regenerate it cheaply before committing to the days."""
        plan = self.planner.plan(req, profile=profile)
        return self.expand(plan, req, profile=profile)

    def expand(
        self, plan: UnitPlan, req: UnitRequest, *, profile: TeacherProfile | None = None
    ) -> UnitDesignDocument:
        """Expand a (possibly teacher-edited) plan into a full unit, sequentially so
        each day sees the real objectives of the days before it."""
        days: list[LessonDesignDocument] = []
        for day_plan in plan.days:
            days.append(self._expand_one(day_plan, plan, req, days, profile))
        udd = UnitDesignDocument(
            title=plan.title,
            curriculum_ref=plan.curriculum_ref,
            big_idea=plan.big_idea,
            unit_outcomes=plan.unit_outcomes,
            plan=plan,
            days=days,
        )
        return self.coherence.apply(udd)

    def regenerate_day(
        self,
        udd: UnitDesignDocument,
        day: int,
        req: UnitRequest,
        *,
        profile: TeacherProfile | None = None,
    ) -> UnitDesignDocument:
        """Rebuild one day (1-based) against the same arc, leaving the others
        untouched, then re-run coherence over the whole unit."""
        if not 1 <= day <= len(udd.days):
            raise ValueError(f"day {day} is out of range 1..{len(udd.days)}")
        day_plan = udd.plan.days[day - 1]
        prior = udd.days[: day - 1]  # only earlier days are the arc context
        rebuilt = self._expand_one(day_plan, udd.plan, req, prior, profile)
        new_days = list(udd.days)
        new_days[day - 1] = rebuilt
        return self.coherence.apply(udd.model_copy(update={"days": new_days}))

    # ── one day ───────────────────────────────────────────────────────────────
    def _expand_one(self, day_plan, plan, req, prior_days, profile) -> LessonDesignDocument:
        brief = self._day_brief(day_plan, plan, req, prior_days, profile)
        draft = self.generator.generate(brief)
        return self.reviser.revise(draft, brief)

    def _day_brief(
        self, day_plan, plan: UnitPlan, req: UnitRequest,
        prior_days: list[LessonDesignDocument], profile: TeacherProfile | None,
    ) -> NormalizedBrief:
        prior = "; ".join(
            f"Day {i} ({d.topic}): " + ", ".join(o.statement for o in d.objectives)
            for i, d in enumerate(prior_days, 1)
        )
        lines = [
            (f"This is Day {day_plan.day} of a {len(plan.days)}-day unit titled "
             f"{plan.title!r}."),
            f"Unit big idea: {plan.big_idea}." if plan.big_idea else "",
            f"Carry this running example through the lesson: {plan.thread}." if plan.thread else "",
            f"This day builds on: {day_plan.builds_on}." if day_plan.builds_on else "",
            f"This day sets up: {day_plan.sets_up}." if day_plan.sets_up else "",
            f"Objective focus for this day: {', '.join(day_plan.objective_seeds)}."
            if day_plan.objective_seeds else "",
            f"Earlier days already covered — {prior}. Do NOT re-teach these; build on them."
            if prior else "",
        ]
        brief = NormalizedBrief(
            topic=day_plan.topic,
            grade=req.grade,
            subject=req.subject,
            duration_min=_coerce_duration(req.duration_min),  # type: ignore[arg-type]
            language=req.language,  # type: ignore[arg-type]
            framework=req.framework,  # type: ignore[arg-type]
            unit_context="\n".join(line for line in lines if line),
        )
        return profile.personalize(brief) if profile is not None else brief
