"""Types for the human-in-the-loop refine step: a teacher's targeted reprompt.

Where the critique→revise loop (see :mod:`~lessonforge.services.revise`) is
*automatic* — the rubric decides what is weak and the best draft is kept — refine
is *human-driven*: the teacher names a section and says how to improve it, and the
result is **proposed, never applied**. The UI shows the diff and score delta and
the teacher accepts or rejects. That is why :class:`RefineResult` carries both the
candidate and the before/after scores, and why ``applied`` is always ``False``
coming out of the service — acceptance is the client's decision.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from .ldd import LessonDesignDocument
from .rubric import RubricScores


class RefineRequest(BaseModel):
    """A teacher's targeted reprompt: improve ``target`` per ``instruction``.

    ``target`` is a section name (see :data:`~lessonforge.domain.sections.LDD_SECTIONS`)
    or ``"*"`` for the whole lesson. ``instruction`` is free-text intent, e.g.
    "make the hook about the local river" or "add a decomposer misconception"."""

    target: str
    instruction: str = Field(min_length=1)


class RefineResult(BaseModel):
    """The outcome of one reprompt — a *proposal*, not a mutation.

    On success ``ok`` is true, ``candidate`` holds the improved (re-validated) LDD,
    ``diff`` maps each changed section to its before/after, and ``score_after``
    lets the UI show the quality delta. On failure ``ok`` is false, ``candidate``
    is ``None``, and ``errors`` explains why (a malformed section, or a coupling
    the change would break that the bounded cascade could not repair)."""

    ok: bool
    target: str
    instruction: str
    candidate: LessonDesignDocument | None = None
    diff: dict[str, Any] = Field(default_factory=dict)
    score_before: RubricScores = Field(default_factory=RubricScores)
    score_after: RubricScores | None = None
    cascaded: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    applied: bool = False  # propose-only; the client accepts the candidate itself
    notes: str = ""
