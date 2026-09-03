"""The reusable model→domain boundary: raw LLM output → a valid pydantic object,
or a clean failure — for *any* target model, not just the LDD.

This is the one hardened path that turns untrusted model text into a strict domain
object, stacked as defense layers so every generator (lesson, quiz, worksheet,
slides) shares exactly one implementation instead of re-inventing validation:

    text ──▶ parse JSON (lenient)
         ──▶ normalize   (optional, deterministic, meaning-preserving)
         ──▶ validate     (the model's own guardrails — the real gate)
         ──▶ repair       (on failure, feed pydantic's errors back to the model,
                           bounded retries; normalize + validate each round)
         ──▶ degrade      (never raise past here — return a structured result)

When the backend guarantees valid output at decode time
(``llm.supports_structured_output``) the repair loop is skipped.

:class:`~lessonforge.services.assemble.LDDAssembler` is a thin wrapper over this
that supplies the LDD model + its normalizer; artifact generators call
:func:`assemble_model` with their own IR and (optionally) a light normalizer.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ValidationError

from ..providers.base import LLMClient
from ..util import extract_json

T = TypeVar("T", bound=BaseModel)

# (data) -> (normalized_data, notes). Must never raise (see normalize.py).
Normalizer = Callable[[Any], "tuple[Any, list[str]]"]

_REPAIR_SYSTEM = (
    "You fix invalid JSON. You are given JSON that failed schema validation and the "
    "exact errors. You change ONLY what the errors require, preserve everything else "
    "verbatim, keep the same JSON structure and ids, and output ONLY the corrected JSON."
)

_REPAIR_PROMPT = """\
The JSON below failed validation. Fix ONLY these problems, changing nothing else:

{errors}
{rules}
JSON to fix:
{json}

Return the full corrected JSON only, no prose.
"""


@dataclass
class AssembleResult(Generic[T]):
    """Outcome of turning model output into a validated object. ``ok`` says whether
    a valid object was produced; ``notes`` records every normalization/repair
    applied; ``errors`` explains the failure when ``ok`` is false."""

    ok: bool
    obj: T | None = None
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


def assemble_model(
    source: str | dict[str, Any],
    *,
    model: type[T],
    llm: LLMClient | None = None,
    normalizer: Normalizer | None = None,
    max_repairs: int = 1,
    repair_rules: str = "",
) -> AssembleResult[T]:
    """Parse → normalize → validate → repair → degrade, for ``model``."""
    notes: list[str] = []
    try:
        data: Any = source if isinstance(source, dict) else extract_json(source)
    except ValueError as exc:
        return AssembleResult(ok=False, errors=[f"model did not return JSON: {exc}"])

    if normalizer is not None:
        data, n = normalizer(data)
        notes += n

    # A backend with true guided decoding can't emit invalid output, so the repair
    # budget is wasted there — skip straight to a single validate.
    budget = 0 if (llm and llm.supports_structured_output) else max(0, max_repairs)

    last_errors: list[str] = []
    attempt = 0
    for attempt in range(budget + 1):
        try:
            obj = model.model_validate(data)
            return AssembleResult(ok=True, obj=obj, repairs=attempt, notes=notes)
        except ValidationError as exc:
            last_errors = error_lines(exc)
            if attempt >= budget or llm is None:
                break
            repaired = _repair(llm, model, data, last_errors, repair_rules)
            if repaired is None:
                break
            if normalizer is not None:
                repaired, n = normalizer(repaired)
                notes += n
            data = repaired

    return AssembleResult(
        ok=False, repairs=attempt, notes=notes,
        errors=last_errors or [f"could not assemble a valid {model.__name__}"],
    )


def _repair(
    llm: LLMClient, model: type[BaseModel], data: Any, errors: list[str], rules: str
) -> Any | None:
    prompt = _REPAIR_PROMPT.format(
        errors="\n".join(f"- {e}" for e in errors),
        rules=f"\n{rules}\n" if rules.strip() else "",
        json=json.dumps(data, ensure_ascii=False, indent=2),
    )
    try:
        result = llm.complete(
            prompt, system=_REPAIR_SYSTEM,
            json_schema=model.model_json_schema(), temperature=0.2,
        )
        return extract_json(result.text)
    except Exception:
        return None  # non-JSON / network hiccup → give up, degrade gracefully
