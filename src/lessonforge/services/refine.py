"""The refine engine — reprompt one section of an LDD, keep the document valid.

This is the human-driven counterpart to the automatic :class:`~lessonforge.services.revise.Reviser`.
Both improve an LDD with the LLM; they differ only in *driver* (rubric weakness vs.
a teacher's instruction) and *application policy* (auto keep-if-better vs. propose
a diff for the teacher to accept). They compose: ``generate`` runs the auto loop to
produce a baseline, then a teacher refines individual sections on top of it.

Design (the senior-engineer bits):

- **Broad context, narrow output.** The model *reads* the whole lesson (so a hook
  can reference the objectives) but *writes back only the target section*. Scoped
  output is cheaper, scales to bigger documents, and — critically — yields a clean
  one-section diff for accept/reject instead of a noisy whole-document rewrite.
- **Two-layer validation.** A returned section is validated first against its own
  sub-schema (is it well-formed?), then spliced into a copy of the LDD and
  re-validated as a whole (does it still satisfy the coupling guardrails?).
- **Bounded cascade.** When a scoped change breaks a coupling — e.g. a new
  objective that nothing assesses — the reviser turns the validator's own error
  into a follow-up reprompt of the coupled neighbour (``formative_checks``), up to
  ``max_cascade`` hops. If it still doesn't validate, the change is *rejected with
  the reason*, never forced through. The guardrails always win.

Every failure path degrades rather than crashes: no LLM, non-JSON, a malformed
section, or an unrepairable coupling all return ``ok=False`` with an explanation.
"""

from __future__ import annotations

import json
from typing import Any

from ..config import RefineConfig
from ..domain.ldd import LessonDesignDocument
from ..domain.refine import RefineRequest, RefineResult
from ..domain.rubric import RubricScores
from ..domain.sections import (
    LDD_SECTIONS,
    WHOLE_DOCUMENT,
    Section,
    first_error,
    fragment_schema,
    is_valid_target,
    validate_fragment,
)
from ..providers.base import LLMClient
from ..util import extract_json
from .critique import Critic

# A sentinel distinct from ``None`` — because ``None`` is a legitimate section
# value (e.g. removing homework), we can't use it to mean "the model failed".
_FAILED: Any = object()

_SECTION_SYSTEM = (
    "You are an experienced Nepali teacher improving ONE section of a lesson plan. "
    "You read the whole lesson for context but rewrite ONLY the section you are asked "
    "to, matching its JSON schema exactly. You output ONLY valid JSON."
)

_SECTION_PROMPT = """\
Improve ONLY the "{target}" section of this lesson, following the teacher's instruction.
Keep it consistent with the rest of the lesson, which you must NOT rewrite.

Teacher's instruction:
{instruction}

The whole lesson (context — do not return this):
{context}

Current "{target}" section:
{current}

Return ONLY this JSON object, where the value matches the section's schema
({schema}):
{{"section": <the improved {target}>}}
"""

_WHOLE_SYSTEM = (
    "You are an experienced Nepali teacher revising a whole lesson plan to follow a "
    "teacher's instruction. You keep the EXACT same JSON schema and keep objective "
    "ids stable, with every objective still taught in a phase and assessed by a "
    "formative check. You output ONLY valid JSON."
)

_WHOLE_PROMPT = """\
Revise this lesson to follow the teacher's instruction, keeping the exact same JSON
schema and keeping every objective both taught (in a phase) and assessed (in a
formative check).

Teacher's instruction:
{instruction}

Current lesson (JSON):
{ldd}

Return the full improved lesson as a single JSON object with the same keys. JSON only.
"""


class Refiner:
    """Reprompt a section (or the whole lesson) and propose a re-validated LDD."""

    def __init__(
        self,
        *,
        llm: LLMClient | None,
        critic: Critic,
        sections: dict[str, Section] | None = None,
        max_cascade: int = 2,
    ) -> None:
        self.llm = llm
        self.critic = critic
        self.sections = sections or LDD_SECTIONS
        self.max_cascade = max(0, max_cascade)

    @classmethod
    def from_config(
        cls, cfg: RefineConfig, *, llm: LLMClient | None, critic: Critic
    ) -> Refiner:
        return cls(llm=llm, critic=critic, max_cascade=cfg.max_cascade)

    # ── public API ───────────────────────────────────────────────────────────
    def refine(self, ldd: LessonDesignDocument, request: RefineRequest) -> RefineResult:
        target = request.target
        if not is_valid_target(target, self.sections):
            available = ", ".join(sorted(self.sections))
            raise ValueError(
                f"unknown refine target {target!r}. Available: {available}, "
                f"or {WHOLE_DOCUMENT!r} for the whole lesson."
            )

        before = self.critic.critique(ldd)
        if self.llm is None:
            return self._reject(
                request, before.scores, ["no LLM configured; refine is unavailable"]
            )

        if target == WHOLE_DOCUMENT:
            candidate, cascaded, errors = self._refine_whole(ldd, request.instruction)
        else:
            candidate, cascaded, errors = self._refine_section(
                ldd, target, request.instruction
            )

        if candidate is None:
            return self._reject(request, before.scores, errors, cascaded)

        # Provenance is authoritative: a reprompt can improve wording but must not
        # invent citations. Carry the original grounding sources across verbatim.
        candidate.quality.grounding_sources = ldd.quality.grounding_sources
        after = self.critic.critique(candidate)
        note = f"refined {target}; overall {before.overall:.2f} → {after.overall:.2f}"
        if cascaded:
            note += f"; cascaded into {', '.join(cascaded)}"
        return RefineResult(
            ok=True,
            target=target,
            instruction=request.instruction,
            candidate=candidate,
            diff=_diff_sections(ldd, candidate),
            score_before=before.scores,
            score_after=after.scores,
            cascaded=cascaded,
            notes=note,
        )

    # ── scoped refine: narrow output, two-layer validation, bounded cascade ───
    def _refine_section(
        self, ldd: LessonDesignDocument, target: str, instruction: str
    ) -> tuple[LessonDesignDocument | None, list[str], list[str]]:
        base = ldd.model_dump(mode="json")
        fragment = self._ask_fragment(target, instruction, base)
        if fragment is _FAILED:
            return None, [], [f"the model did not return valid JSON for {target!r}"]

        err = validate_fragment(target, fragment)
        if err:
            return None, [], [f"proposed {target!r} is invalid: {err}"]

        working = {**base, target: fragment}
        candidate, violation = _try_whole(working)
        if candidate is not None:
            return candidate, [], []

        # The scoped change broke a coupling guardrail. Repair the declared
        # neighbours in a bounded cascade, driven by the validator's own message.
        cascaded: list[str] = []
        for neighbour in self.sections[target].coupled[: self.max_cascade]:
            n_instr = _cascade_instruction(target, neighbour, violation)
            n_fragment = self._ask_fragment(neighbour, n_instr, working)
            if n_fragment is _FAILED or validate_fragment(neighbour, n_fragment):
                break
            working = {**working, neighbour: n_fragment}
            cascaded.append(neighbour)
            candidate, violation = _try_whole(working)
            if candidate is not None:
                return candidate, cascaded, []

        return None, cascaded, [f"this change breaks the lesson's structure: {violation}"]

    def _ask_fragment(
        self, target: str, instruction: str, context: dict[str, Any]
    ) -> Any:
        prompt = _SECTION_PROMPT.format(
            target=target,
            instruction=instruction,
            context=json.dumps(context, ensure_ascii=False, indent=2),
            current=json.dumps(context.get(target), ensure_ascii=False, indent=2),
            schema=json.dumps(fragment_schema(target)),
        )
        try:
            result = self.llm.complete(  # type: ignore[union-attr]
                prompt, system=_SECTION_SYSTEM, temperature=0.4
            )
            data = extract_json(result.text)
        except Exception:
            return _FAILED
        if not isinstance(data, dict) or "section" not in data:
            return _FAILED
        return data["section"]

    # ── whole-document refine: the rare cross-cutting instruction ─────────────
    def _refine_whole(
        self, ldd: LessonDesignDocument, instruction: str
    ) -> tuple[LessonDesignDocument | None, list[str], list[str]]:
        prompt = _WHOLE_PROMPT.format(
            instruction=instruction, ldd=ldd.model_dump_json(indent=2)
        )
        try:
            result = self.llm.complete(  # type: ignore[union-attr]
                prompt,
                system=_WHOLE_SYSTEM,
                json_schema=LessonDesignDocument.model_json_schema(),
                temperature=0.4,
            )
            candidate = LessonDesignDocument.model_validate(extract_json(result.text))
        except Exception as exc:
            return None, [], [f"the model's rewrite was not a valid lesson: {exc}"]
        return candidate, [], []

    def _reject(
        self,
        request: RefineRequest,
        score_before: RubricScores,
        errors: list[str],
        cascaded: list[str] | None = None,
    ) -> RefineResult:
        return RefineResult(
            ok=False,
            target=request.target,
            instruction=request.instruction,
            score_before=score_before,
            cascaded=cascaded or [],
            errors=errors,
            notes="; ".join(errors),
        )


def _try_whole(data: dict[str, Any]) -> tuple[LessonDesignDocument | None, str]:
    """Layer-2 check: does the spliced document satisfy the coupling guardrails?"""
    from pydantic import ValidationError

    try:
        return LessonDesignDocument.model_validate(data), ""
    except ValidationError as exc:
        return None, first_error(exc)


def _cascade_instruction(target: str, neighbour: str, violation: str) -> str:
    return (
        f"The {target!r} section was just revised, which left the lesson "
        f"inconsistent: {violation}. Revise the {neighbour!r} section so every "
        f"objective stays both taught (in a phase) and assessed (in a formative "
        f"check). Change only what is needed and keep existing ids stable."
    )


def _diff_sections(
    before: LessonDesignDocument, after: LessonDesignDocument
) -> dict[str, Any]:
    """Per-section before/after for every top-level field that changed (except the
    stamped ``quality`` block). Scoped output means this is normally one entry."""
    b = before.model_dump(mode="json")
    a = after.model_dump(mode="json")
    out: dict[str, Any] = {}
    for name in LessonDesignDocument.model_fields:
        if name == "quality":
            continue
        if b.get(name) != a.get(name):
            out[name] = {"before": b.get(name), "after": a.get(name)}
    return out
