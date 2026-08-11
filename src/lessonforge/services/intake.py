"""The intake stage — raw request (possibly a pasted plan) → NormalizedBrief.

This is US-3: a teacher pastes their rough existing plan and the tool *enriches*
it rather than replacing it. Intake's job is to normalize that messy input into a
clean :class:`NormalizedBrief` — pulling out grade, subject, duration, language,
and topic — while preserving the pasted text (``existing_plan``) so the
enrichment stage can build on it instead of discarding it.

Two strategies behind the ``Intake`` port:

- ``HeuristicIntake`` — deterministic regex/keyword extraction. No model, no cost;
  the default seam for tests and offline use.
- ``LLMIntake`` — the fast model extracts the fields, with ``HeuristicIntake`` as
  a graceful fallback on any failure.

In both, an explicitly-provided field always wins over anything parsed from the
pasted plan; sensible defaults (45 min, ``en-ne``, ``5E``) fill the rest.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import Any

from ..config import IntakeConfig
from ..domain.ldd import IntakeRequest, NormalizedBrief
from ..providers.base import LLMClient
from ..util import extract_json
from .registry import register_intake

_VALID_DURATIONS = (30, 45, 60)
# Subjects intake can recognize in free text (extend freely — order = priority).
_KNOWN_SUBJECTS = (
    "Science",
    "Mathematics",
    "Math",
    "English",
    "Nepali",
    "Social Studies",
    "Health",
    "Physical Education",
    "Computer",
    "Environment",
)
_GRADE_RE = re.compile(r"(?:grade|class|कक्षा)\s*[:\-]?\s*(\d{1,2})", re.IGNORECASE)
_DURATION_RE = re.compile(r"(\d{2,3})\s*(?:min|mins|minutes|मिनेट)", re.IGNORECASE)
_TOPIC_RE = re.compile(r"(?:topic|lesson|title|unit|chapter)\s*[:\-]\s*(.+)", re.IGNORECASE)


def _snap_duration(minutes: int | None) -> int | None:
    if minutes is None:
        return None
    return min(_VALID_DURATIONS, key=lambda v: abs(v - minutes))


class Intake(ABC):
    """Normalizes raw input into a NormalizedBrief."""

    @abstractmethod
    def normalize(self, request: IntakeRequest) -> NormalizedBrief:
        ...

    @classmethod
    def from_config(cls, cfg: IntakeConfig, *, llm: LLMClient) -> Intake:  # pragma: no cover
        raise NotImplementedError

    @staticmethod
    def _merge(request: IntakeRequest, extracted: dict[str, Any]) -> NormalizedBrief:
        """Explicit request fields win; extracted fields fill gaps; then defaults."""

        def pick(field: str, default: Any) -> Any:
            explicit = getattr(request, field)
            if explicit is not None:
                return explicit
            val = extracted.get(field)
            return val if val is not None else default

        topic = pick("topic", None)
        if not topic:
            raise ValueError(
                "intake could not determine a topic — provide `topic` or a plan that names one"
            )
        grade = pick("grade", None)
        if grade is None:
            raise ValueError(
                "intake could not determine a grade — provide `grade` or a plan that names one"
            )
        return NormalizedBrief(
            topic=str(topic).strip(),
            grade=int(grade),
            subject=str(pick("subject", "General")).strip(),
            duration_min=_snap_duration(pick("duration_min", None)) or 45,
            language=pick("language", "en-ne"),
            framework=pick("framework", "5E"),
            existing_plan=request.existing_plan,
        )


@register_intake("heuristic")
class HeuristicIntake(Intake):
    """Deterministic extraction from a pasted plan — regex + keyword matching."""

    @classmethod
    def from_config(cls, cfg: IntakeConfig, *, llm: LLMClient) -> HeuristicIntake:
        return cls()

    def normalize(self, request: IntakeRequest) -> NormalizedBrief:
        return self._merge(request, self.extract(request.existing_plan or ""))

    def extract(self, plan: str) -> dict[str, Any]:
        if not plan.strip():
            return {}
        out: dict[str, Any] = {}
        if m := _GRADE_RE.search(plan):
            out["grade"] = int(m.group(1))
        if m := _DURATION_RE.search(plan):
            out["duration_min"] = int(m.group(1))
        for subj in _KNOWN_SUBJECTS:
            if re.search(rf"\b{re.escape(subj)}\b", plan, re.IGNORECASE):
                out["subject"] = "Mathematics" if subj == "Math" else subj
                break
        if m := _TOPIC_RE.search(plan):
            out["topic"] = m.group(1).strip()
        else:
            # fall back to the first non-empty line as the topic
            for line in plan.splitlines():
                if line.strip():
                    out["topic"] = line.strip().lstrip("#").strip()
                    break
        return out


_INTAKE_SYSTEM = (
    "You extract structured lesson metadata from a teacher's rough notes or plan. "
    "You output ONLY valid JSON and never invent a topic that is not present."
)

_INTAKE_PROMPT = """\
Extract lesson metadata from the plan below. Return ONLY this JSON shape; use null
for anything not stated (do not guess grade or subject if absent):
{{"topic": "...", "grade": 6, "subject": "Science", "duration_min": 45,
  "language": "en-ne", "framework": "5E"}}

Plan:
{plan}
"""


@register_intake("llm")
class LLMIntake(Intake):
    """Fast-model extraction, with heuristic extraction as a graceful fallback."""

    def __init__(self, *, llm: LLMClient) -> None:
        self.llm = llm
        self._fallback = HeuristicIntake()

    @classmethod
    def from_config(cls, cfg: IntakeConfig, *, llm: LLMClient) -> LLMIntake:
        return cls(llm=llm)

    def normalize(self, request: IntakeRequest) -> NormalizedBrief:
        plan = request.existing_plan or ""
        # No pasted plan and enough explicit fields → no need to call the model.
        if not plan.strip():
            return self._merge(request, {})
        return self._merge(request, self._extract(plan))

    def _extract(self, plan: str) -> dict[str, Any]:
        try:
            result = self.llm.complete(
                _INTAKE_PROMPT.format(plan=plan), system=_INTAKE_SYSTEM, temperature=0.0
            )
            data: dict[str, Any] = extract_json(result.text)
            # keep only known keys with non-null values; snap duration if present
            allowed = {"topic", "grade", "subject", "duration_min", "language", "framework"}
            extracted = {k: v for k, v in data.items() if k in allowed and v is not None}
            # a heuristic pass fills anything the model missed
            merged = {**self._fallback.extract(plan), **extracted}
            return merged
        except Exception:
            return self._fallback.extract(plan)
