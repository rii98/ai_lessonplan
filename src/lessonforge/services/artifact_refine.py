"""AI "Improve" for artifact IRs — the artifact counterpart of
:class:`~lessonforge.services.refine.Refiner`.

Same engine as the LDD refiner — broad context, narrow output, two-layer
validation, bounded cascade — but generic over any artifact IR (quiz/worksheet/
slides) and driven by a per-IR section map. Two differences from the LDD refiner:

- **No rubric.** Artifacts have no anti-generic critique, so the result carries a
  diff (for accept/reject) but no score delta.
- **Scoped re-grounding.** When the edited section is factual
  (:attr:`~lessonforge.domain.sections.Section.grounded`), the refiner re-retrieves
  grounding with a query scoped to the *instruction + the edited content* and
  **merges** the new sources into the candidate — so an AI change stays anchored in
  the reference material and its citation stays real (never stale, never
  fabricated). Purely structural edits (a title, instructions) skip retrieval.

Every failure path degrades rather than crashes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError

from ..domain.sections import (
    Section,
    first_error,
    fragment_schema_for,
    is_valid_target,
    validate_fragment_for,
)
from ..providers.base import LLMClient
from ..rag.grounding import GroundingRetriever, merge_sources
from ..util import extract_json

WHOLE_DOCUMENT = "*"
_FAILED: Any = object()


@dataclass
class ArtifactRefineResult:
    """A *proposal*, not a mutation — the client accepts or rejects the candidate."""

    ok: bool
    target: str
    instruction: str
    candidate: BaseModel | None = None
    diff: dict[str, Any] = field(default_factory=dict)
    cascaded: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    notes: str = ""


_SECTION_SYSTEM = (
    "You are an experienced Nepali teacher improving ONE part of a teaching "
    "artifact (a quiz, worksheet, or slide deck). You read the whole artifact for "
    "context but rewrite ONLY the part you are asked to, matching its JSON schema "
    "exactly. You output ONLY valid JSON."
)

_SECTION_PROMPT = """\
Improve ONLY the "{target}" part of this {kind}, following the teacher's instruction.
Keep it consistent with the rest, which you must NOT rewrite.
{grounding}
Teacher's instruction:
{instruction}

The whole {kind} (context — do not return this):
{context}

Current "{target}":
{current}

Return ONLY this JSON object, where the value matches the schema ({schema}):
{{"section": <the improved {target}>}}
"""

_WHOLE_SYSTEM = (
    "You are an experienced Nepali teacher revising a whole teaching artifact to "
    "follow a teacher's instruction. You keep the EXACT same JSON schema and keep "
    "ids stable. You output ONLY valid JSON."
)

_WHOLE_PROMPT = """\
Revise this {kind} to follow the teacher's instruction, keeping the exact same JSON
schema.
{grounding}
Teacher's instruction:
{instruction}

Current {kind} (JSON):
{doc}

Return the full improved {kind} as one JSON object with the same keys. JSON only.
"""


class ArtifactRefiner:
    def __init__(
        self,
        *,
        kind: str,
        model: type[BaseModel],
        sections: dict[str, Section],
        llm: LLMClient | None,
        grounding: GroundingRetriever | None = None,
        max_cascade: int = 2,
    ) -> None:
        self.kind = kind
        self.model = model
        self.sections = sections
        self.llm = llm
        self.grounding = grounding
        self.max_cascade = max(0, max_cascade)

    # ── public API ───────────────────────────────────────────────────────────
    def refine(self, obj: BaseModel, target: str, instruction: str) -> ArtifactRefineResult:
        if not is_valid_target(target, self.sections):
            available = ", ".join(sorted(self.sections))
            raise ValueError(
                f"unknown refine target {target!r}. Available: {available}, "
                f"or {WHOLE_DOCUMENT!r} for the whole {self.kind}."
            )
        if self.llm is None:
            return self._reject(target, instruction, ["no LLM configured; refine is unavailable"])

        if target == WHOLE_DOCUMENT:
            candidate, cascaded, errors = self._refine_whole(obj, instruction)
        else:
            candidate, cascaded, errors = self._refine_section(obj, target, instruction)

        if candidate is None:
            return self._reject(target, instruction, errors, cascaded)

        self._reground(candidate, obj, target, instruction)
        note = f"refined {target}"
        if cascaded:
            note += f"; cascaded into {', '.join(cascaded)}"
        return ArtifactRefineResult(
            ok=True, target=target, instruction=instruction, candidate=candidate,
            diff=_diff(self.model, obj, candidate), cascaded=cascaded, notes=note,
        )

    # ── scoped refine ─────────────────────────────────────────────────────────
    def _refine_section(
        self, obj: BaseModel, target: str, instruction: str
    ) -> tuple[BaseModel | None, list[str], list[str]]:
        base = obj.model_dump(mode="json")
        fragment = self._ask_fragment(target, instruction, base, ground=self.sections[target].grounded)
        if fragment is _FAILED:
            return None, [], [f"the model did not return valid JSON for {target!r}"]
        err = validate_fragment_for(self.model, target, fragment)
        if err:
            return None, [], [f"proposed {target!r} is invalid: {err}"]

        working = {**base, target: fragment}
        candidate, violation = self._try_whole(working)
        if candidate is not None:
            return candidate, [], []

        cascaded: list[str] = []
        for neighbour in self.sections[target].coupled[: self.max_cascade]:
            n_instr = _cascade_instruction(target, neighbour, violation)
            n_fragment = self._ask_fragment(neighbour, n_instr, working, ground=False)
            if n_fragment is _FAILED or validate_fragment_for(self.model, neighbour, n_fragment):
                break
            working = {**working, neighbour: n_fragment}
            cascaded.append(neighbour)
            candidate, violation = self._try_whole(working)
            if candidate is not None:
                return candidate, cascaded, []
        return None, cascaded, [f"this change breaks the {self.kind}'s structure: {violation}"]

    def _ask_fragment(
        self, target: str, instruction: str, context: dict[str, Any], *, ground: bool
    ) -> Any:
        prompt = _SECTION_PROMPT.format(
            kind=self.kind, target=target, instruction=instruction,
            grounding=self._grounding_block(context, instruction) if ground else "",
            context=json.dumps(context, ensure_ascii=False, indent=2),
            current=json.dumps(context.get(target), ensure_ascii=False, indent=2),
            schema=json.dumps(fragment_schema_for(self.model, target)),
        )
        try:
            result = self.llm.complete(prompt, system=_SECTION_SYSTEM, temperature=0.4)  # type: ignore[union-attr]
            data = extract_json(result.text)
        except Exception:
            return _FAILED
        if not isinstance(data, dict) or "section" not in data:
            return _FAILED
        return data["section"]

    def _refine_whole(
        self, obj: BaseModel, instruction: str
    ) -> tuple[BaseModel | None, list[str], list[str]]:
        prompt = _WHOLE_PROMPT.format(
            kind=self.kind, instruction=instruction,
            grounding=self._grounding_block(obj.model_dump(mode="json"), instruction),
            doc=obj.model_dump_json(indent=2),
        )
        try:
            result = self.llm.complete(  # type: ignore[union-attr]
                prompt, system=_WHOLE_SYSTEM,
                json_schema=self.model.model_json_schema(), temperature=0.4,
            )
            candidate = self.model.model_validate(extract_json(result.text))
        except Exception as exc:
            return None, [], [f"the model's rewrite was not a valid {self.kind}: {exc}"]
        return candidate, [], []

    def _try_whole(self, data: dict[str, Any]) -> tuple[BaseModel | None, str]:
        try:
            return self.model.model_validate(data), ""
        except ValidationError as exc:
            return None, first_error(exc)

    # ── scoped re-grounding (the senior-call: keep AI edits grounded) ─────────
    def _grounding_block(self, doc: dict[str, Any], instruction: str) -> str:
        """A short grounding-context block injected into the reprompt, retrieved
        with a query scoped to the instruction — so the AI's new content is drawn
        from the reference book, not invented."""
        bundle = self._retrieve(doc, instruction)
        if bundle is None or bundle.is_empty:
            return ""
        return "\n" + bundle.as_prompt_context() + "\n"

    def _reground(
        self, candidate: BaseModel, before: BaseModel, target: str, instruction: str
    ) -> None:
        """Merge the sources actually retrieved for this edit into the candidate,
        so citations grow to cover the new content (never shrink, never fabricated).
        Skipped for non-grounded (cosmetic) targets."""
        if target != WHOLE_DOCUMENT and not self.sections[target].grounded:
            return
        if not hasattr(candidate, "grounding_sources"):
            return
        bundle = self._retrieve(candidate.model_dump(mode="json"), instruction)
        new = bundle.sources if bundle else []
        candidate.grounding_sources = merge_sources(candidate.grounding_sources, new)

    def _retrieve(self, doc: dict[str, Any], instruction: str):
        if self.grounding is None:
            return None
        ref = doc.get("curriculum_ref") or {}
        query = f"{instruction} {doc.get('topic', '')}"[:400]
        try:
            return self.grounding.ground(
                query=query, grade=ref.get("grade"), subject=ref.get("subject")
            )
        except Exception:
            return None

    def _reject(
        self, target: str, instruction: str, errors: list[str], cascaded: list[str] | None = None
    ) -> ArtifactRefineResult:
        return ArtifactRefineResult(
            ok=False, target=target, instruction=instruction,
            cascaded=cascaded or [], errors=errors, notes="; ".join(errors),
        )


def _cascade_instruction(target: str, neighbour: str, violation: str) -> str:
    return (
        f"The {target!r} part was just revised, which left the artifact "
        f"inconsistent: {violation}. Revise the {neighbour!r} part so every "
        f"question still maps to a real objective. Change only what is needed and "
        f"keep existing ids stable."
    )


def _diff(model: type[BaseModel], before: BaseModel, after: BaseModel) -> dict[str, Any]:
    """Per-field before/after for every changed field except the stamped
    ``grounding_sources`` — normally one entry (scoped output)."""
    b = before.model_dump(mode="json")
    a = after.model_dump(mode="json")
    out: dict[str, Any] = {}
    for name in model.model_fields:
        if name == "grounding_sources":
            continue
        if b.get(name) != a.get(name):
            out[name] = {"before": b.get(name), "after": a.get(name)}
    return out
