"""The critic strategies and their registry."""

from __future__ import annotations

import pytest

from lessonforge.config import CritiqueConfig
from lessonforge.providers.base import LLMResult
from lessonforge.services.critique import (
    CompositeCritic,
    LLMCritic,
    NoopCritic,
    StructuralCritic,
)
from lessonforge.services.registry import build_critic
from tests.conftest import FakeLLM


def test_structural_critic_scores_good_lesson_high(export_ldd):
    critique = StructuralCritic().critique(export_ldd)
    assert critique.overall >= 0.7
    # a rich lesson covers misconceptions and local context
    assert critique.scores.misconception_coverage > 0.0
    assert critique.scores.local_relevance > 0.0


def test_structural_critic_scores_weak_lesson_low(weak_ldd):
    critique = StructuralCritic().critique(weak_ldd)
    assert critique.overall < 0.7
    assert critique.scores.misconception_coverage == 0.0
    assert critique.scores.local_relevance == 0.0
    # weak sections come back with concrete rewrite advice
    assert "misconceptions" in critique.suggestions


def test_structural_critic_is_deterministic(export_ldd):
    a = StructuralCritic().critique(export_ldd)
    b = StructuralCritic().critique(export_ldd)
    assert a.overall == b.overall and a.scores.as_dict() == b.scores.as_dict()


def test_noop_critic_passes_everything(weak_ldd):
    critique = NoopCritic().critique(weak_ldd)
    assert critique.overall == 1.0


def test_llm_critic_parses_scores(export_ldd):
    llm = FakeLLM(response={
        "scores": {"engagement": 0.9, "alignment": 0.9, "misconception_coverage": 0.8,
                   "specificity": 0.8, "local_relevance": 0.9},
        "suggestions": {"engagement_hook": "tie it to the local market"},
    })
    critique = LLMCritic(llm=llm).critique(export_ldd)
    assert critique.scores.engagement == 0.9
    assert critique.suggestions["engagement_hook"]


def test_llm_critic_falls_back_to_structural_on_bad_json(export_ldd):
    class BadLLM(FakeLLM):
        def complete(self, *a, **k):
            return LLMResult(text="not json")

    llm_critique = LLMCritic(llm=BadLLM()).critique(export_ldd)
    structural = StructuralCritic().critique(export_ldd)
    assert llm_critique.scores.as_dict() == structural.scores.as_dict()


def test_composite_critic_averages_both(export_ldd):
    llm = FakeLLM(response={"scores": {"engagement": 0.0, "alignment": 0.0,
                  "misconception_coverage": 0.0, "specificity": 0.0, "local_relevance": 0.0}})
    composite = CompositeCritic(llm=llm).critique(export_ldd)
    structural = StructuralCritic().critique(export_ldd)
    # mean of structural and an all-zero LLM score → strictly below structural alone
    assert composite.overall < structural.overall


def test_build_critic_from_registry():
    critic = build_critic(CritiqueConfig(provider="structural"), llm=FakeLLM())
    assert isinstance(critic, StructuralCritic)


def test_build_critic_unknown_provider_fails_loudly():
    with pytest.raises(ValueError, match="Unknown critic provider"):
        build_critic(CritiqueConfig(provider="nope"), llm=FakeLLM())
