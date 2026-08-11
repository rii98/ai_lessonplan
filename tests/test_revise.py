"""The revise loop — improve to threshold, keep the best, always stamp scores."""

from __future__ import annotations

from lessonforge.services.critique import NoopCritic, StructuralCritic
from lessonforge.services.revise import Reviser
from tests.conftest import FakeLLM


def test_reviser_noop_when_already_passing(weak_ldd):
    llm = FakeLLM(response={})
    reviser = Reviser(llm=llm, critic=NoopCritic(), threshold=0.7, max_iterations=1)
    out = reviser.revise(weak_ldd)
    assert out is weak_ldd  # unchanged
    assert out.quality.engagement == 1.0  # scores still stamped
    assert llm.calls == []  # passing draft → no rewrite call


def test_reviser_improves_weak_lesson(weak_ldd, export_ldd_dict):
    # the LLM's "rewrite" returns a much stronger lesson
    llm = FakeLLM(response=export_ldd_dict)
    reviser = Reviser(llm=llm, critic=StructuralCritic(), threshold=0.7, max_iterations=1)
    out = reviser.revise(weak_ldd)
    assert out.topic == export_ldd_dict["topic"]  # the improved draft was kept
    assert "1 revision" in out.quality.notes
    assert out.quality.misconception_coverage > 0.0


def test_reviser_keeps_best_when_no_improvement(weak_ldd, weak_ldd_dict):
    # rewrite returns an equally-weak lesson → rejected, original kept
    llm = FakeLLM(response=weak_ldd_dict)
    reviser = Reviser(llm=llm, critic=StructuralCritic(), threshold=0.7, max_iterations=1)
    out = reviser.revise(weak_ldd)
    assert out is weak_ldd
    assert "0 revision" in out.quality.notes


def test_reviser_keeps_best_when_rewrite_is_invalid(weak_ldd):
    from lessonforge.providers.base import LLMResult

    class BadLLM(FakeLLM):
        def complete(self, *a, **k):
            return LLMResult(text="{ not valid json")

    reviser = Reviser(llm=BadLLM(), critic=StructuralCritic(), threshold=0.7, max_iterations=1)
    out = reviser.revise(weak_ldd)
    assert out is weak_ldd  # bad rewrite discarded, degrade gracefully


def test_reviser_without_llm_is_noop_but_stamps(weak_ldd):
    reviser = Reviser(llm=None, critic=StructuralCritic(), threshold=0.7, max_iterations=2)
    out = reviser.revise(weak_ldd)
    assert out is weak_ldd
    assert "below bar" in out.quality.notes  # scored, just not revised


def test_reviser_preserves_grounding_sources_across_rewrite(weak_ldd, export_ldd_dict):
    weak_ldd.quality.grounding_sources = ["CDC Science Grade 6, Unit X"]
    # the rewrite response omits grounding_sources; the reviser must re-stamp them
    payload = {**export_ldd_dict, "quality": {"grounding_sources": [], "notes": ""}}
    reviser = Reviser(llm=FakeLLM(response=payload), critic=StructuralCritic(),
                      threshold=0.7, max_iterations=1)
    out = reviser.revise(weak_ldd)
    assert out.quality.grounding_sources == ["CDC Science Grade 6, Unit X"]
