"""The LDD model→domain boundary: raw LLM output → a valid LDD, or a clean failure.

This is a thin, LDD-flavoured wrapper over the reusable
:func:`~lessonforge.services.model_assembler.assemble_model` boundary (parse →
normalize → validate → bounded repair → degrade). It supplies the LDD model, the
LDD normalizer (:func:`~lessonforge.services.normalize.normalize_ldd`), and the
LDD-specific repair rules, and preserves the original :class:`AssembleOutcome`
API (``.ldd``) so every existing caller and test is untouched. Artifact
generators reuse the *same* boundary directly with their own IR — so the hardened
path is written once, not per artifact.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..domain.ldd import LessonDesignDocument
from ..providers.base import LLMClient
from .model_assembler import assemble_model, error_lines
from .normalize import normalize_ldd

__all__ = ["AssembleOutcome", "LDDAssembler", "error_lines"]

_REPAIR_RULES = (
    "Rules that must hold in the result:\n"
    "- engagement_hook.kind is one of: scenario, question, prediction, demonstration.\n"
    "- Every objective id appears in at least one phase AND one formative check.\n"
    "- MCQ questions include an \"options\" list; other question types set \"options\": null."
)


@dataclass
class AssembleOutcome:
    """The result of turning model output into an LDD. ``ok`` says whether a valid
    LDD was produced; ``notes`` records every normalization/repair applied (for
    transparency); ``errors`` explains the failure when ``ok`` is false."""

    ok: bool
    ldd: LessonDesignDocument | None = None
    repairs: int = 0
    notes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class LDDAssembler:
    def __init__(self, *, llm: LLMClient | None = None, max_repairs: int = 1) -> None:
        self.llm = llm
        self.max_repairs = max(0, max_repairs)

    def assemble(self, source: str | dict[str, Any]) -> AssembleOutcome:
        result = assemble_model(
            source,
            model=LessonDesignDocument,
            llm=self.llm,
            normalizer=normalize_ldd,
            max_repairs=self.max_repairs,
            repair_rules=_REPAIR_RULES,
        )
        return AssembleOutcome(
            ok=result.ok,
            ldd=result.obj,
            repairs=result.repairs,
            notes=result.notes,
            errors=result.errors,
        )
