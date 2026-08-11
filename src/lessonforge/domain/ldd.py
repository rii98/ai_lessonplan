"""The Lesson Design Document (LDD) — the validated intermediate representation.

This is the single source of truth from which every export (plan, slides,
worksheet, quiz) is compiled. Its validators are the anti-generic guardrails:
a lesson with no hook, no active learning, or an assessment that doesn't map to
an objective simply fails to construct.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class BloomLevel(str, Enum):
    remember = "remember"
    understand = "understand"
    apply = "apply"
    analyze = "analyze"
    evaluate = "evaluate"
    create = "create"


class QuestionType(str, Enum):
    mcq = "mcq"
    true_false = "true_false"
    short_answer = "short_answer"


class CurriculumRef(BaseModel):
    board: Literal["CDC", "NEB"] = "CDC"
    grade: int
    subject: str
    code: str | None = None  # e.g. a CDC learning-outcome code, when known


class Objective(BaseModel):
    id: str
    statement: str
    bloom: BloomLevel

    @field_validator("statement")
    @classmethod
    def _measurable(cls, v: str) -> str:
        if len(v.split()) < 3:
            raise ValueError("objective statement is too vague to be measurable")
        return v


class Misconception(BaseModel):
    statement: str
    correction: str
    source: str | None = None  # provenance from retrieval


class Hook(BaseModel):
    """Engagement move. Must be a scenario/question, never a definition."""

    prompt: str
    kind: Literal["scenario", "question", "prediction", "demonstration"] = "question"

    @field_validator("prompt")
    @classmethod
    def _not_a_definition(cls, v: str) -> str:
        lowered = v.strip().lower()
        if lowered.startswith(("definition", "the definition", "define ")):
            raise ValueError("hook must engage curiosity, not open with a definition")
        return v


class Phase(BaseModel):
    name_en: str
    name_ne: str = ""  # bilingual pedagogical label, e.g. "संलग्न गराउनु"
    teacher_activities: list[str] = Field(min_length=1)
    student_activities: list[str] = Field(min_length=1)
    minutes: int = Field(gt=0)
    objective_ids: list[str] = Field(default_factory=list)


class Differentiation(BaseModel):
    struggling: list[str] = Field(default_factory=list)
    on_level: list[str] = Field(default_factory=list)
    advanced: list[str] = Field(default_factory=list)


class Question(BaseModel):
    id: str
    type: QuestionType
    prompt: str
    answer: str
    objective_ids: list[str] = Field(min_length=1)
    options: list[str] | None = None  # for MCQ

    @model_validator(mode="after")
    def _mcq_needs_options(self) -> Question:
        if self.type is QuestionType.mcq and not self.options:
            raise ValueError("MCQ questions require options")
        return self


class Homework(BaseModel):
    instructions: list[str] = Field(min_length=1)
    objective_ids: list[str] = Field(default_factory=list)


class QualityReport(BaseModel):
    engagement: float = 0.0
    alignment: float = 0.0
    misconception_coverage: float = 0.0
    specificity: float = 0.0
    local_relevance: float = 0.0
    grounding_sources: list[str] = Field(default_factory=list)
    notes: str = ""


class LessonDesignDocument(BaseModel):
    # identity & framing
    topic: str
    curriculum_ref: CurriculumRef
    duration_min: Literal[30, 45, 60]
    language: Literal["en", "ne", "en-ne"] = "en-ne"
    framework: Literal["5E", "gradual_release", "inquiry"] = "5E"

    # the teacher's reasoning
    objectives: list[Objective] = Field(min_length=1)
    prior_knowledge: list[str] = Field(default_factory=list)
    misconceptions: list[Misconception] = Field(default_factory=list)
    engagement_hook: Hook
    local_context: list[str] = Field(default_factory=list)

    # the sequence
    phases: list[Phase] = Field(min_length=1)
    materials: list[str] = Field(default_factory=list)
    differentiation: Differentiation = Field(default_factory=Differentiation)

    # assessment
    formative_checks: list[Question] = Field(default_factory=list)
    homework: Homework | None = None

    quality: QualityReport = Field(default_factory=QualityReport)

    # ── anti-generic structural guardrails ──────────────────────────────────
    @model_validator(mode="after")
    def _objective_ids_unique(self) -> LessonDesignDocument:
        ids = [o.id for o in self.objectives]
        if len(ids) != len(set(ids)):
            raise ValueError("objective ids must be unique")
        return self

    @model_validator(mode="after")
    def _phases_reference_real_objectives(self) -> LessonDesignDocument:
        valid = {o.id for o in self.objectives}
        for p in self.phases:
            unknown = set(p.objective_ids) - valid
            if unknown:
                raise ValueError(f"phase {p.name_en!r} references unknown objectives {unknown}")
        for q in self.formative_checks:
            unknown = set(q.objective_ids) - valid
            if unknown:
                raise ValueError(f"question {q.id!r} references unknown objectives {unknown}")
        return self

    @model_validator(mode="after")
    def _every_objective_is_taught_and_assessed(self) -> LessonDesignDocument:
        taught = {oid for p in self.phases for oid in p.objective_ids}
        assessed = {oid for q in self.formative_checks for oid in q.objective_ids}
        for o in self.objectives:
            if o.id not in taught:
                raise ValueError(f"objective {o.id!r} is not addressed by any phase")
            if o.id not in assessed:
                raise ValueError(f"objective {o.id!r} is not covered by any formative check")
        return self


class NormalizedBrief(BaseModel):
    """Output of the intake stage; input to enrichment."""

    topic: str
    grade: int
    subject: str
    duration_min: Literal[30, 45, 60] = 45
    language: Literal["en", "ne", "en-ne"] = "en-ne"
    framework: Literal["5E", "gradual_release", "inquiry"] = "5E"
    existing_plan: str | None = None

    # personalization stamped by the TeacherProfile (see ``domain/profile.py``);
    # enrichment reads these to match the teacher's voice and local examples.
    style_notes: str = ""
    local_anchors: list[str] = Field(default_factory=list)


class IntakeRequest(BaseModel):
    """Raw input to the intake stage — every field optional so a teacher can
    paste an existing plan and let intake extract the rest (US-3). Explicit
    fields always win over anything parsed from the pasted plan."""

    topic: str | None = None
    grade: int | None = None
    subject: str | None = None
    duration_min: Literal[30, 45, 60] | None = None
    language: Literal["en", "ne", "en-ne"] | None = None
    framework: Literal["5E", "gradual_release", "inquiry"] | None = None
    existing_plan: str | None = None
