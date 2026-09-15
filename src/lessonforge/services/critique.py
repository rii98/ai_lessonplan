"""The critique stage — score an LDD against the anti-generic rubric.

A :class:`Critic` turns a validated LDD into a :class:`Critique` (per-dimension
scores + rewrite guidance). The reviser (see :mod:`.revise`) uses that to decide
whether — and where — to rewrite.

Three strategies, all behind the ``Critic`` port and chosen by one config line:

- ``StructuralCritic`` — deterministic heuristics over the LDD's structure. No
  LLM, no cost, fully reproducible. This is the default and the test seam.
- ``LLMCritic`` — asks the reasoning model to judge the lesson on the rubric.
  Falls back to the structural score on any failure (graceful degradation).
- ``CompositeCritic`` — the mean of both, unioning their suggestions.
- ``NoopCritic`` — scores everything 1.0, i.e. disables critique.

The structural heuristics deliberately never award a perfect score to a merely
*valid* lesson: the LDD validators already guarantee structural soundness, so the
rubric measures the softer qualities (a real hook, misconceptions, specific
local activities) that separate a good lesson from a generic one.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from ..config import CritiqueConfig
from ..domain.ldd import Hook, LessonDesignDocument
from ..domain.rubric import DIMENSION_SECTIONS, RUBRIC_DIMENSIONS, Critique, RubricScores
from ..providers.base import LLMClient
from ..util import extract_json
from .registry import register_critic


class Critic(ABC):
    """Scores an LDD against the rubric."""

    def __init__(self, *, weights: dict[str, float] | None = None) -> None:
        self.weights = weights or {}

    @abstractmethod
    def score(self, ldd: LessonDesignDocument) -> tuple[RubricScores, dict[str, str]]:
        """Return raw per-dimension scores and per-section rewrite suggestions."""

    def critique(self, ldd: LessonDesignDocument) -> Critique:
        scores, suggestions = self.score(ldd)
        overall = scores.weighted_overall(self.weights)
        notes = "; ".join(
            f"{dim}={getattr(scores, dim):.2f}" for dim in RUBRIC_DIMENSIONS
        )
        return Critique(scores=scores, overall=overall, suggestions=suggestions, notes=notes)

    @classmethod
    def from_config(cls, cfg: CritiqueConfig, *, llm: LLMClient) -> Critic:  # pragma: no cover
        raise NotImplementedError


# ── deterministic heuristics ─────────────────────────────────────────────────
def _avg_words(items: list[str]) -> float:
    lengths = [len(s.split()) for s in items if s.strip()]
    return sum(lengths) / len(lengths) if lengths else 0.0


def _mentions_any(text: str, terms: list[str]) -> bool:
    low = text.lower()
    return any(t.lower() in low for t in terms if t.strip())


@register_critic("structural")
class StructuralCritic(Critic):
    """Deterministic rubric scoring from the LDD's structure alone."""

    @classmethod
    def from_config(cls, cfg: CritiqueConfig, *, llm: LLMClient) -> StructuralCritic:
        return cls(weights=cfg.weights)

    def score(self, ldd: LessonDesignDocument) -> tuple[RubricScores, dict[str, str]]:
        scores = RubricScores(
            engagement=self._engagement(ldd),
            alignment=self._alignment(ldd),
            misconception_coverage=self._misconceptions(ldd),
            specificity=self._specificity(ldd),
            local_relevance=self._local_relevance(ldd),
        )
        suggestions = {
            DIMENSION_SECTIONS[dim]: _SECTION_ADVICE[dim]
            for dim in RUBRIC_DIMENSIONS
            if getattr(scores, dim) < 0.7
        }
        return scores, suggestions

    def _engagement(self, ldd: LessonDesignDocument) -> float:
        kind_base = {"demonstration": 0.9, "scenario": 0.9, "prediction": 0.85, "question": 0.6}
        hook: Hook = ldd.engagement_hook
        s = kind_base.get(hook.kind, 0.6)
        # a hook grounded in the local world, or an open question, is stronger
        if "?" in hook.prompt or _mentions_any(hook.prompt, ldd.local_context):
            s += 0.1
        return s

    def _alignment(self, ldd: LessonDesignDocument) -> float:
        # Full objective→phase→check coverage is already guaranteed by the LDD
        # validators, so start high and reward assessment depth + bloom variety.
        base = 0.7
        n_obj = len(ldd.objectives)
        depth = min(1.0, len(ldd.formative_checks) / n_obj) if n_obj else 0.0
        bloom_variety = len({o.bloom for o in ldd.objectives}) / n_obj if n_obj else 0.0
        return base + 0.2 * depth + 0.1 * bloom_variety

    def _misconceptions(self, ldd: LessonDesignDocument) -> float:
        corrected = [m for m in ldd.misconceptions if m.correction.strip()]
        return min(1.0, len(corrected) / 2.0)

    def _specificity(self, ldd: LessonDesignDocument) -> float:
        activities = [a for p in ldd.phases for a in (p.teacher_activities + p.student_activities)]
        activity_signal = min(1.0, _avg_words(activities) / 8.0)
        materials_signal = min(1.0, len(ldd.materials) / 3.0)
        homework_signal = 1.0 if ldd.homework else 0.0
        return 0.5 * activity_signal + 0.3 * materials_signal + 0.2 * homework_signal

    def _local_relevance(self, ldd: LessonDesignDocument) -> float:
        n_signal = min(1.0, len(ldd.local_context) / 3.0)
        activities = " ".join(
            a for p in ldd.phases for a in (p.teacher_activities + p.student_activities)
        )
        used = _mentions_any(activities, ldd.local_context) or _mentions_any(
            ldd.engagement_hook.prompt, ldd.local_context
        )
        return 0.5 * n_signal + 0.5 * (1.0 if used else 0.0)


_SECTION_ADVICE: dict[str, str] = {
    "engagement": "Open with a concrete local scenario, prediction, or demonstration — never a definition.",
    "alignment": "Add a formative check for every objective and vary Bloom levels across objectives.",
    "misconception_coverage": "Surface at least two grade-specific misconceptions, each with its correction.",
    "specificity": "Make activities concrete and step-by-step; name real materials; add homework.",
    "local_relevance": "Ground hooks and activities in the students' own daily life and surroundings, with anchors that genuinely fit this topic and grade.",
}


# ── LLM judge ────────────────────────────────────────────────────────────────
_CRITIC_SYSTEM = (
    "You are a strict but fair teacher-trainer reviewing a lesson plan. You score "
    "on five dimensions from 0.0 to 1.0 and never inflate: a merely-valid but "
    "generic lesson scores low on engagement, specificity, and local relevance. "
    "You output ONLY valid JSON."
)

_CRITIC_PROMPT = """\
Score this lesson plan (JSON) on each rubric dimension from 0.0 (poor) to 1.0 (excellent):

- engagement: is the hook a real curiosity move (scenario/question/demo), not a definition?
- alignment: is every objective both taught and assessed, with appropriate cognitive depth?
- misconception_coverage: are grade-specific misconceptions surfaced and corrected?
- specificity: are activities concrete and actionable, not vague filler?
- local_relevance: is it grounded in the students' local (Nepali) world?

Lesson:
{ldd}

Return ONLY this JSON shape (floats in [0,1]); `suggestions` maps a weak section
name to one concrete fix (omit sections that are already strong):
{{"scores": {{"engagement": 0.0, "alignment": 0.0, "misconception_coverage": 0.0,
  "specificity": 0.0, "local_relevance": 0.0}},
  "suggestions": {{"engagement_hook": "..."}}}}
"""


@register_critic("llm")
class LLMCritic(Critic):
    """LLM-judge scoring, with the structural critic as a graceful fallback."""

    def __init__(self, *, llm: LLMClient, weights: dict[str, float] | None = None) -> None:
        super().__init__(weights=weights)
        self.llm = llm
        self._fallback = StructuralCritic(weights=weights)

    @classmethod
    def from_config(cls, cfg: CritiqueConfig, *, llm: LLMClient) -> LLMCritic:
        return cls(llm=llm, weights=cfg.weights)

    def score(self, ldd: LessonDesignDocument) -> tuple[RubricScores, dict[str, str]]:
        try:
            result = self.llm.complete(
                _CRITIC_PROMPT.format(ldd=ldd.model_dump_json(indent=2)),
                system=_CRITIC_SYSTEM,
                temperature=0.2,
            )
            data: dict[str, Any] = extract_json(result.text)
            scores = RubricScores.model_validate(data.get("scores", data))
            suggestions = {str(k): str(v) for k, v in (data.get("suggestions") or {}).items()}
            return scores, suggestions
        except Exception:
            # A flaky judge must not sink the request — fall back to structure.
            return self._fallback.score(ldd)


@register_critic("composite")
class CompositeCritic(Critic):
    """Mean of the structural and LLM critics; unions their suggestions."""

    def __init__(self, *, llm: LLMClient, weights: dict[str, float] | None = None) -> None:
        super().__init__(weights=weights)
        self.structural = StructuralCritic(weights=weights)
        self.llm_critic = LLMCritic(llm=llm, weights=weights)

    @classmethod
    def from_config(cls, cfg: CritiqueConfig, *, llm: LLMClient) -> CompositeCritic:
        return cls(llm=llm, weights=cfg.weights)

    def score(self, ldd: LessonDesignDocument) -> tuple[RubricScores, dict[str, str]]:
        s1, sug1 = self.structural.score(ldd)
        s2, sug2 = self.llm_critic.score(ldd)
        mean = RubricScores(
            **{dim: (getattr(s1, dim) + getattr(s2, dim)) / 2.0 for dim in RUBRIC_DIMENSIONS}
        )
        return mean, {**sug1, **sug2}


@register_critic("noop")
class NoopCritic(Critic):
    """Disables critique: everything passes. Useful to turn the loop off."""

    @classmethod
    def from_config(cls, cfg: CritiqueConfig, *, llm: LLMClient) -> NoopCritic:
        return cls(weights=cfg.weights)

    def score(self, ldd: LessonDesignDocument) -> tuple[RubricScores, dict[str, str]]:
        return RubricScores(**{dim: 1.0 for dim in RUBRIC_DIMENSIONS}), {}
