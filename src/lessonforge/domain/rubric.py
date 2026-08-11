"""The critique rubric — the anti-generic quality signal, as data.

The five dimensions are exactly those the SRD calls out (engagement, alignment,
misconception coverage, specificity, local relevance) and mirror the fields on
:class:`~lessonforge.domain.ldd.QualityReport`, so a :class:`Critique` stamps
straight back onto the LDD. Scores are always in ``[0, 1]``; anything a critic
(deterministic or LLM) produces outside that range is clamped rather than
rejected, so a stray model number degrades the score instead of crashing the run.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator

# The rubric dimensions, in a fixed order. Each maps to the LDD section a reviser
# would rewrite to improve it (see ``DIMENSION_SECTIONS``).
RUBRIC_DIMENSIONS: tuple[str, ...] = (
    "engagement",
    "alignment",
    "misconception_coverage",
    "specificity",
    "local_relevance",
)

# Which LDD section a weak dimension points the reviser at.
DIMENSION_SECTIONS: dict[str, str] = {
    "engagement": "engagement_hook",
    "alignment": "objectives / phases / formative_checks",
    "misconception_coverage": "misconceptions",
    "specificity": "phases (teacher/student activities) and materials",
    "local_relevance": "local_context",
}


def _clamp(v: float) -> float:
    return 0.0 if v < 0 else 1.0 if v > 1 else float(v)


class RubricScores(BaseModel):
    """One score per rubric dimension, each in ``[0, 1]``."""

    engagement: float = 0.0
    alignment: float = 0.0
    misconception_coverage: float = 0.0
    specificity: float = 0.0
    local_relevance: float = 0.0

    @model_validator(mode="after")
    def _clamp_all(self) -> RubricScores:
        for dim in RUBRIC_DIMENSIONS:
            setattr(self, dim, _clamp(getattr(self, dim)))
        return self

    def as_dict(self) -> dict[str, float]:
        return {dim: getattr(self, dim) for dim in RUBRIC_DIMENSIONS}

    def weighted_overall(self, weights: dict[str, float] | None = None) -> float:
        """Weighted mean across dimensions. Missing/zero weights fall back to
        equal weighting so a partial ``weights`` map can never divide by zero."""
        w = {dim: 1.0 for dim in RUBRIC_DIMENSIONS}
        if weights:
            for dim, val in weights.items():
                if dim in w and val >= 0:
                    w[dim] = float(val)
        total = sum(w.values())
        if total <= 0:  # all weights zeroed → treat as equal weighting
            w = {dim: 1.0 for dim in RUBRIC_DIMENSIONS}
            total = float(len(RUBRIC_DIMENSIONS))
        return sum(getattr(self, dim) * w[dim] for dim in RUBRIC_DIMENSIONS) / total

    def weak_dimensions(self, threshold: float) -> list[str]:
        """Dimensions scoring strictly below ``threshold`` — the reviser's targets."""
        return [dim for dim in RUBRIC_DIMENSIONS if getattr(self, dim) < threshold]


class Critique(BaseModel):
    """A scored assessment of one LDD: per-dimension scores, a weighted overall,
    and (optionally) targeted rewrite guidance per weak section."""

    scores: RubricScores
    overall: float = 0.0
    suggestions: dict[str, str] = Field(default_factory=dict)
    notes: str = ""

    @model_validator(mode="after")
    def _clamp_overall(self) -> Critique:
        self.overall = _clamp(self.overall)
        return self
