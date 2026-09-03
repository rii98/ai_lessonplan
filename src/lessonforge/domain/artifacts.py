"""Standalone artifact IRs — the source of truth for a *targeted* generation.

The :class:`~lessonforge.domain.ldd.LessonDesignDocument` is the source of truth
for a *whole lesson*; these smaller documents are the source of truth for a single
artifact a teacher asked for on its own — "just a quiz", "just a worksheet", "just
the slides". Each:

- reuses the LDD's own building blocks (:class:`Objective`, :class:`Question`,
  :class:`CurriculumRef`) so validation and vocabulary never diverge;
- carries its own light guardrails (a quiz maps every question to a real
  objective, a deck has at least one slide);
- exposes :meth:`from_ldd` — a byte-faithful projection so an artifact can be
  extracted from an already-generated lesson, and :meth:`coerce` so a renderer
  can accept either the standalone IR or a full LDD interchangeably.

That dual path is the whole point: generate an artifact *standalone* (cheap, no
fabricated lesson) OR project it from a lesson that already exists — same IR, same
renderer, both routes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, Field, model_validator

from .ldd import CurriculumRef, Objective, Question

if TYPE_CHECKING:
    from .ldd import LessonDesignDocument


def _questions_map_to_objectives(objectives: list[Objective], questions: list[Question]) -> None:
    """Shared guardrail (mirrors the LDD's alignment check, lighter): every
    question must reference a declared objective, so a targeted artifact stays
    aligned to what it claims to assess instead of drifting into trivia."""
    valid = {o.id for o in objectives}
    for q in questions:
        unknown = set(q.objective_ids) - valid
        if unknown:
            raise ValueError(f"question {q.id!r} references unknown objectives {unknown}")


class Quiz(BaseModel):
    """A standalone quiz: questions with an answer key, aligned to objectives."""

    topic: str
    curriculum_ref: CurriculumRef
    objectives: list[Objective] = Field(min_length=1)
    questions: list[Question] = Field(min_length=1)
    instructions: str = ""
    # provenance of the material this artifact was grounded in (real sources
    # retrieved, not model-invented) — mirrors the LDD's quality.grounding_sources.
    grounding_sources: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _aligned(self) -> Quiz:
        _questions_map_to_objectives(self.objectives, self.questions)
        return self

    @classmethod
    def from_ldd(cls, ldd: LessonDesignDocument) -> Quiz:
        return cls(
            topic=ldd.topic,
            curriculum_ref=ldd.curriculum_ref,
            objectives=ldd.objectives,
            questions=ldd.formative_checks,
            grounding_sources=ldd.quality.grounding_sources,
        )

    @classmethod
    def coerce(cls, source: Quiz | LessonDesignDocument) -> Quiz:
        return source if isinstance(source, cls) else cls.from_ldd(source)


class Worksheet(BaseModel):
    """A standalone worksheet: objectives, hands-on tasks, and practice questions
    with an answer key. At least one task or question must be present."""

    topic: str
    curriculum_ref: CurriculumRef
    objectives: list[Objective] = Field(min_length=1)
    tasks: list[str] = Field(default_factory=list)
    questions: list[Question] = Field(default_factory=list)
    grounding_sources: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _has_work_and_is_aligned(self) -> Worksheet:
        if not self.tasks and not self.questions:
            raise ValueError("a worksheet needs at least one task or question")
        _questions_map_to_objectives(self.objectives, self.questions)
        return self

    @classmethod
    def from_ldd(cls, ldd: LessonDesignDocument) -> Worksheet:
        tasks = list(ldd.homework.instructions) if ldd.homework else []
        return cls(
            topic=ldd.topic,
            curriculum_ref=ldd.curriculum_ref,
            objectives=ldd.objectives,
            tasks=tasks,
            questions=ldd.formative_checks,
            grounding_sources=ldd.quality.grounding_sources,
        )

    @classmethod
    def coerce(cls, source: Worksheet | LessonDesignDocument) -> Worksheet:
        return source if isinstance(source, cls) else cls.from_ldd(source)


class Slide(BaseModel):
    """One slide: a heading, optional muted subtitle, and bullet lines."""

    heading: str
    subtitle: str | None = None
    bullets: list[str] = Field(default_factory=list)


class Slides(BaseModel):
    """A standalone classroom deck. The renderer emits a title slide from
    ``topic``/``subtitle`` then one slide per :class:`Slide`, so a deck stays
    hook-first when the slides are ordered that way."""

    topic: str
    curriculum_ref: CurriculumRef
    subtitle: str = ""
    slides: list[Slide] = Field(min_length=1)
    grounding_sources: list[str] = Field(default_factory=list)

    @classmethod
    def from_ldd(cls, ldd: LessonDesignDocument) -> Slides:
        r = ldd.curriculum_ref
        subtitle = (f"{r.board} · Grade {r.grade} · {r.subject}   ·   "
                    f"{ldd.duration_min} min · {ldd.framework}")
        slides: list[Slide] = [
            # hook FIRST — the whole point of hook-first
            Slide(heading="Let's begin…", subtitle=f"({ldd.engagement_hook.kind})",
                  bullets=[ldd.engagement_hook.prompt]),
            Slide(heading="What we'll be able to do",
                  bullets=[o.statement for o in ldd.objectives]),
        ]
        for i, phase in enumerate(ldd.phases, 1):
            label = phase.name_en if not phase.name_ne else f"{phase.name_en} — {phase.name_ne}"
            body = [f"▸ {a}" for a in phase.teacher_activities]
            body += [f"• {a}" for a in phase.student_activities]
            slides.append(Slide(heading=f"{i}. {label}", subtitle=f"{phase.minutes} min",
                                bullets=body))
        if ldd.formative_checks:
            slides.append(Slide(heading="Check for Understanding",
                                bullets=[q.prompt for q in ldd.formative_checks]))
        return cls(topic=ldd.topic, curriculum_ref=ldd.curriculum_ref,
                   subtitle=subtitle, slides=slides,
                   grounding_sources=ldd.quality.grounding_sources)

    @classmethod
    def coerce(cls, source: Slides | LessonDesignDocument) -> Slides:
        return source if isinstance(source, cls) else cls.from_ldd(source)
