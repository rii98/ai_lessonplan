"""The rubric model: clamping, weighting, weak-dimension selection."""

from __future__ import annotations

from lessonforge.domain.rubric import RUBRIC_DIMENSIONS, Critique, RubricScores


def test_scores_clamp_to_unit_range():
    s = RubricScores(engagement=1.5, alignment=-0.2, specificity=0.5)
    assert s.engagement == 1.0
    assert s.alignment == 0.0
    assert s.specificity == 0.5


def test_weighted_overall_equal_weighting_is_mean():
    s = RubricScores(**{d: 0.5 for d in RUBRIC_DIMENSIONS})
    assert s.weighted_overall() == 0.5


def test_weighted_overall_respects_weights():
    s = RubricScores(engagement=1.0)  # others 0
    # weight engagement heavily → overall pulled toward 1.0
    heavy = s.weighted_overall({"engagement": 9.0})
    equal = s.weighted_overall()
    assert heavy > equal


def test_weighted_overall_all_zero_weights_falls_back_to_equal():
    s = RubricScores(**{d: 0.4 for d in RUBRIC_DIMENSIONS})
    assert s.weighted_overall({d: 0.0 for d in RUBRIC_DIMENSIONS}) == 0.4


def test_weak_dimensions_below_threshold():
    s = RubricScores(engagement=0.9, alignment=0.9, misconception_coverage=0.0,
                     specificity=0.5, local_relevance=0.0)
    weak = s.weak_dimensions(0.7)
    assert set(weak) == {"misconception_coverage", "specificity", "local_relevance"}


def test_critique_clamps_overall():
    c = Critique(scores=RubricScores(), overall=2.0)
    assert c.overall == 1.0
