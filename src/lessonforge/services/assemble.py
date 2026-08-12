"""The model→domain boundary: raw LLM output → a valid LDD, or a clean failure.

This is the one place untrusted model output is turned into a strict domain
object, and it stacks the defense layers so every stage (generation, whole-lesson
refine) can share exactly one hardened path instead of re-implementing validation:

    text ──▶ parse JSON (lenient)
         ──▶ L1 normalize   (deterministic, meaning-preserving; see normalize.py)
         ──▶ validate        (the LDD guardrails — the real gate)
         ──▶ L2 repair       (on failure, feed pydantic's errors back to the model,
                              bounded retries; normalize + validate each round)
         ──▶ L3 degrade      (never raise past here — return a structured outcome)

L1 removes cosmetic deviations for free; L2 handles the genuinely semantic ones
(an unassessed objective) using the validator's own message as the fix
instruction — the same validator-driven re-prompt pattern as the refine cascade.
When the backend guarantees valid output at decode time
(``llm.supports_structured_output``), the repair loop is skipped entirely.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from ..domain.ldd import LessonDesignDocument
from ..providers.base import LLMClient
from ..util import extract_json
from .normalize import normalize_ldd

_REPAIR_SYSTEM = (
    "You fix invalid lesson-plan JSON. You are given JSON that failed schema "
    "validation and the exact errors. You change ONLY what the errors require, "
    "preserve everything else verbatim, keep the same JSON structure and ids, and "
    "output ONLY the corrected JSON."
)

_REPAIR_PROMPT = """\
The JSON below failed validation. Fix ONLY these problems, changing nothing else:

{errors}

Rules that must hold in the result:
- engagement_hook.kind is one of: scenario, question, prediction, demonstration.
- Every objective id appears in at least one phase AND one formative check.
- MCQ questions include an "options" list; other question types set "options": null.

JSON to fix:
{json}

Return the full corrected JSON only, no prose.
"""


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


def error_lines(exc: ValidationError) -> list[str]:
    """Flatten pydantic errors into concise ``loc: msg`` lines usable both as a
    repair instruction and as a user-facing explanation."""
    out: list[str] = []
    for e in exc.errors():
        loc = ".".join(str(p) for p in e["loc"])
        out.append(f"{loc}: {e['msg']}" if loc else str(e["msg"]))
    return out


class LDDAssembler:
    def __init__(self, *, llm: LLMClient | None = None, max_repairs: int = 1) -> None:
        self.llm = llm
        self.max_repairs = max(0, max_repairs)

    def assemble(self, source: str | dict[str, Any]) -> AssembleOutcome:
        notes: list[str] = []
        try:
            data: Any = source if isinstance(source, dict) else extract_json(source)
        except ValueError as exc:
            return AssembleOutcome(ok=False, errors=[f"model did not return JSON: {exc}"])

        data, n = normalize_ldd(data)
        notes += n

        # A backend with true guided decoding can't emit invalid output, so the
        # repair budget is wasted effort there — skip straight to a single validate.
        budget = 0 if (self.llm and self.llm.supports_structured_output) else self.max_repairs

        last_errors: list[str] = []
        for attempt in range(budget + 1):
            try:
                ldd = LessonDesignDocument.model_validate(data)
                return AssembleOutcome(ok=True, ldd=ldd, repairs=attempt, notes=notes)
            except ValidationError as exc:
                last_errors = error_lines(exc)
                if attempt >= budget or self.llm is None:
                    break
                repaired = self._repair(data, last_errors)
                if repaired is None:
                    break
                data, n = normalize_ldd(repaired)
                notes += n

        return AssembleOutcome(ok=False, repairs=attempt, notes=notes,
                               errors=last_errors or ["could not assemble a valid lesson"])

    def _repair(self, data: dict[str, Any], errors: list[str]) -> dict[str, Any] | None:
        prompt = _REPAIR_PROMPT.format(
            errors="\n".join(f"- {e}" for e in errors),
            json=json.dumps(data, ensure_ascii=False, indent=2),
        )
        try:
            result = self.llm.complete(  # type: ignore[union-attr]
                prompt,
                system=_REPAIR_SYSTEM,
                json_schema=LessonDesignDocument.model_json_schema(),
                temperature=0.2,
            )
            return extract_json(result.text)
        except Exception:
            return None  # non-JSON / network hiccup → give up, degrade gracefully
