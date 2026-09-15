"""RetrievalPlanner (Phase 2): pick narrow/section/broad per need, free on the
obvious majority (heuristic), smart on the ambiguous rest (llm/hybrid), cached."""

from __future__ import annotations

from lessonforge.config import PlannerConfig
from lessonforge.providers.base import LLMResult
from lessonforge.rag.planner import (
    HybridPlanner,
    build_retrieval_planner,
)
from tests.conftest import FakeLLM


# ── heuristic: kind wins, then query stems ────────────────────────────────────
def test_kind_maps_directly_to_granularity():
    p = build_retrieval_planner(PlannerConfig(provider="heuristic"))
    assert p.plan("Scientific Study", kind="unit_plan").granularity == "broad"
    assert p.plan("Variables", kind="lesson").granularity == "section"
    assert p.plan("define variable", kind="quiz_item").granularity == "narrow"


def test_query_stems_decide_when_no_kind():
    p = build_retrieval_planner(PlannerConfig(provider="heuristic"))
    assert p.plan("define a variable").granularity == "narrow"
    assert p.plan("explain how pulleys reduce effort").granularity == "section"
    assert p.plan("plan the whole unit across 5 days").granularity == "broad"


def test_unknown_need_uses_default():
    p = build_retrieval_planner(PlannerConfig(provider="heuristic", default_granularity="section"))
    assert p.plan("photosynthesis").granularity == "section"


# ── llm provider: classifies, degrades to heuristic ───────────────────────────
def test_llm_planner_uses_model_answer():
    llm = FakeLLM()
    llm.complete = lambda *a, **k: LLMResult(text="broad")  # type: ignore[method-assign]
    p = build_retrieval_planner(PlannerConfig(provider="llm"), llm=llm)
    assert p.plan("something ambiguous").granularity == "broad"


def test_llm_planner_degrades_on_garbage_answer():
    llm = FakeLLM()
    llm.complete = lambda *a, **k: LLMResult(text="I think maybe a lot?")  # type: ignore[method-assign]
    p = build_retrieval_planner(PlannerConfig(provider="llm"), llm=llm)
    # unrecognized answer → heuristic fallback (a clear narrow stem)
    assert p.plan("define a variable").granularity == "narrow"


def test_llm_planner_without_llm_falls_back_to_heuristic():
    p = build_retrieval_planner(PlannerConfig(provider="llm"), llm=None)
    assert p.plan("plan the whole unit across 5 days").granularity == "broad"


# ── hybrid: heuristic first, LLM only when ambiguous ──────────────────────────
def test_hybrid_does_not_call_llm_when_heuristic_is_confident():
    calls: list[str] = []

    class CountingLLM(FakeLLM):
        def complete(self, prompt, **k):
            calls.append(prompt)
            return LLMResult(text="broad")

    p = HybridPlanner(CountingLLM())
    assert p.plan("define a variable").granularity == "narrow"
    assert calls == []  # confident heuristic → no LLM spent


def test_hybrid_calls_llm_only_when_ambiguous():
    calls: list[str] = []

    class CountingLLM(FakeLLM):
        def complete(self, prompt, **k):
            calls.append(prompt)
            return LLMResult(text="section")

    p = HybridPlanner(CountingLLM())
    plan = p.plan("photosynthesis")  # no stem, no kind → ambiguous
    assert plan.granularity == "section"
    assert len(calls) == 1


# ── caching ───────────────────────────────────────────────────────────────────
def test_plans_are_cached_per_need():
    calls: list[str] = []

    class CountingLLM(FakeLLM):
        def complete(self, prompt, **k):
            calls.append(prompt)
            return LLMResult(text="broad")

    p = build_retrieval_planner(PlannerConfig(provider="llm", cache=True), llm=CountingLLM())
    p.plan("ambiguous need")
    p.plan("ambiguous need")
    assert len(calls) == 1  # second call served from cache


def test_cache_can_be_disabled():
    calls: list[str] = []

    class CountingLLM(FakeLLM):
        def complete(self, prompt, **k):
            calls.append(prompt)
            return LLMResult(text="broad")

    p = build_retrieval_planner(PlannerConfig(provider="llm", cache=False), llm=CountingLLM())
    p.plan("ambiguous need")
    p.plan("ambiguous need")
    assert len(calls) == 2
