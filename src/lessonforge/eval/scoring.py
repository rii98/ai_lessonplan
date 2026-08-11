"""Structural scoring for the eval harness.

The LDD's Pydantic validators already reject anything *structurally invalid*, so
any :class:`LessonDesignDocument` that exists is sound. These metrics measure the
next layer up — properties a good lesson has that a merely-valid one might not:
balanced timing, enough phases, differentiation, homework, assessment depth.

Deterministic and LLM-free, so this half of the eval score is fully reproducible;
the rubric critic supplies the pedagogical-judgement half.
"""

from __future__ import annotations

from ..domain.ldd import LessonDesignDocument

STRUCTURAL_METRICS: tuple[str, ...] = (
    "timing_balance",
    "phase_coverage",
    "assessment_depth",
    "differentiation",
    "homework",
)


def structural_score(ldd: LessonDesignDocument) -> tuple[float, dict[str, float]]:
    """Return the mean structural score and its per-metric breakdown, all in [0, 1]."""
    duration = ldd.duration_min
    total_minutes = sum(p.minutes for p in ldd.phases)
    timing_balance = max(0.0, 1.0 - abs(total_minutes - duration) / duration)

    # 5E wants ~5 phases; reward up to 3 as a floor so 30-min lessons aren't punished.
    phase_coverage = min(1.0, len(ldd.phases) / 3.0)

    n_obj = len(ldd.objectives)
    assessment_depth = min(1.0, len(ldd.formative_checks) / n_obj) if n_obj else 0.0

    diff = ldd.differentiation
    differentiation = 1.0 if (diff.struggling or diff.on_level or diff.advanced) else 0.0

    homework = 1.0 if (ldd.homework and ldd.homework.instructions) else 0.0

    breakdown = {
        "timing_balance": timing_balance,
        "phase_coverage": phase_coverage,
        "assessment_depth": assessment_depth,
        "differentiation": differentiation,
        "homework": homework,
    }
    mean = sum(breakdown.values()) / len(breakdown)
    return mean, breakdown
