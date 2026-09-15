"""The multi-day unit — a *composition* of per-day LDDs, not a replacement for them.

The Grade-10 reference plan is five daily lessons that form one arc: Day 2 reuses
Day 1's pulley, Day 3 plots Day 2's data, Days 4-5 chain the units. We reproduce
that with **plan-and-expand**:

1. a small, validated :class:`UnitPlan` (the "spine") lays out the arc — per-day
   topic, objective seeds, and how each day *builds on* the last and *sets up* the
   next, plus the running example (``thread``) carried across days;
2. each day is expanded into a full :class:`~lessonforge.domain.ldd.LessonDesignDocument`
   by the existing generator, so every per-day guardrail, renderer, refiner, and
   critique keeps working unchanged.

The :class:`UnitDesignDocument` binds the plan and the days together and adds only
*cross-day* structural invariants — the per-day invariants already live on the LDD.
Semantic arc quality (outcome coverage, prior-knowledge continuity, no repeated
hooks) is assessed by the deterministic coherence pass and reported here, not
enforced in the constructor, since failing construction on fragile text matching
would be brittle.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator

from .ldd import CurriculumRef, LessonDesignDocument


class DayPlan(BaseModel):
    """One day's slot in the unit spine — enough to expand it, not the full lesson."""

    day: int = Field(ge=1)
    topic: str
    objective_seeds: list[str] = Field(default_factory=list)
    builds_on: str = ""   # what earlier day/idea this one depends on
    sets_up: str = ""     # what later day/idea this one prepares for


class UnitPlan(BaseModel):
    """The teacher-reviewable spine produced before any expensive expansion. Small
    enough to regenerate cheaply and to eyeball for arc quality."""

    title: str
    curriculum_ref: CurriculumRef
    big_idea: str = ""
    unit_outcomes: list[str] = Field(default_factory=list)
    thread: str = ""  # the running example carried across days (e.g. "pulley")
    days: list[DayPlan] = Field(min_length=1)

    @model_validator(mode="after")
    def _days_are_sequential(self) -> UnitPlan:
        nums = [d.day for d in self.days]
        if nums != list(range(1, len(nums) + 1)):
            raise ValueError("day numbers must be 1..N with no gaps or duplicates")
        return self


class CoherenceIssue(BaseModel):
    """One cross-day problem found by the coherence pass."""

    kind: str            # duplicate_topic | repeated_hook | outcome_gap | minute_budget | continuity
    day: int | None = None
    detail: str = ""


class CoherenceReport(BaseModel):
    """The deterministic arc-quality assessment of a unit (see the coherence pass)."""

    ok: bool = True
    issues: list[CoherenceIssue] = Field(default_factory=list)


class UnitDesignDocument(BaseModel):
    """A whole unit: its framing, the spine it was expanded from, and the per-day
    lessons. Cross-day invariants only — each day's own guardrails live on the LDD."""

    title: str
    curriculum_ref: CurriculumRef
    big_idea: str = ""
    unit_outcomes: list[str] = Field(default_factory=list)
    plan: UnitPlan
    days: list[LessonDesignDocument] = Field(min_length=1)
    coherence: CoherenceReport = Field(default_factory=CoherenceReport)

    @model_validator(mode="after")
    def _days_match_plan(self) -> UnitDesignDocument:
        if len(self.days) != len(self.plan.days):
            raise ValueError(
                f"unit has {len(self.days)} day(s) but the plan lays out "
                f"{len(self.plan.days)}"
            )
        return self

    @model_validator(mode="after")
    def _days_share_grade_and_subject(self) -> UnitDesignDocument:
        g, s = self.curriculum_ref.grade, self.curriculum_ref.subject
        for i, day in enumerate(self.days, 1):
            if day.curriculum_ref.grade != g or day.curriculum_ref.subject != s:
                raise ValueError(
                    f"day {i} is grade {day.curriculum_ref.grade} "
                    f"{day.curriculum_ref.subject}, not the unit's {g} {s}"
                )
        return self


class UnitRequest(BaseModel):
    """Raw input to the unit pipeline: a chapter/topic to spread across N days."""

    topic: str
    grade: int
    subject: str
    num_days: int = Field(default=5, ge=1, le=20)
    duration_min: int = 45
    language: str = "en-ne"
    framework: str = "5E"
