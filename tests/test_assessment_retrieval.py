"""Type-aware retrieval: the plan is a pure function of the blueprint, and the
grounder runs one pass per profile and merges them."""

from __future__ import annotations

from lessonforge.domain.assessment import AssessmentSpec, TypeSpec
from lessonforge.domain.ldd import NormalizedBrief
from lessonforge.rag.grounding import GroundingBundle
from lessonforge.rag.retriever import RetrievedChunk
from lessonforge.services.assessment_retrieval import AssessmentGrounder, plan_passes

_BRIEF = NormalizedBrief(topic="Sound", grade=7, subject="Science")


def _spec(scope="auto", **counts):
    return AssessmentSpec(scope=scope, types=[TypeSpec(type=t, count=n) for t, n in counts.items()])


def test_objective_only_quiz_is_one_narrow_pass():
    (p,) = plan_passes(_spec(mcq=5, true_false=3))
    assert (p.profile, p.granularity) == ("facts", "narrow")


def test_fact_top_n_scales_with_question_count_within_bounds():
    small = plan_passes(_spec(mcq=1))[0].top_n
    mid = plan_passes(_spec(mcq=10))[0].top_n
    huge = plan_passes(_spec(mcq=25, true_false=25))[0].top_n
    assert small == 3 and small < mid <= huge == 8


def test_explanatory_and_numerical_types_get_whole_sections():
    by = {p.profile: p for p in plan_passes(_spec(mcq=4, long_answer=2, numerical=2, ordering=1))}
    assert set(by) == {"facts", "explain", "worked"}
    assert by["facts"].granularity == "narrow"
    assert by["explain"].granularity == by["worked"].granularity == "section"
    assert "worked example" in by["worked"].query_steer


def test_ordering_shares_the_explain_pass_with_long_answer():
    passes = plan_passes(_spec(long_answer=1, ordering=1))
    assert [p.profile for p in passes] == ["explain"]


def test_chapter_scope_is_a_single_broad_pass():
    (p,) = plan_passes(_spec(scope="chapter", mcq=4, long_answer=2, numerical=1))
    assert p.granularity == "broad"


def test_forced_scope_overrides_per_profile_choice():
    assert {p.granularity for p in plan_passes(_spec(scope="focused", mcq=2, long_answer=1))} == {"narrow"}
    assert {p.granularity for p in plan_passes(_spec(scope="section", mcq=2, long_answer=1))} == {"section"}


class _SpyGrounding:
    def __init__(self):
        self.calls = []

    def ground(self, **kw):
        self.calls.append(kw)
        text = f"passage for {kw['granularity']}"
        chunk = RetrievedChunk(text=text, score=1.0, payload={"source": f"src-{kw['granularity']}"})
        return GroundingBundle(chunks={"reference": [chunk]}, sources=[f"src-{kw['granularity']}"])


def test_grounder_runs_each_pass_with_its_granularity_and_merges():
    spy = _SpyGrounding()
    bundle = AssessmentGrounder(spy).ground(_BRIEF, _spec(mcq=4, long_answer=1))
    assert [(c["granularity"], c["top_n"]) for c in spy.calls] == [("narrow", 3), ("section", 2)]
    assert "explanation" in spy.calls[1]["query"] and "explanation" not in spy.calls[0]["query"]
    assert bundle.sources == ["src-narrow", "src-section"]
    assert {c.text for c in bundle.chunks["reference"]} == {"passage for narrow", "passage for section"}


def test_a_failing_pass_does_not_sink_the_others():
    class Flaky(_SpyGrounding):
        def ground(self, **kw):
            if kw["granularity"] == "narrow":
                raise RuntimeError("store down")
            return super().ground(**kw)

    bundle = AssessmentGrounder(Flaky()).ground(_BRIEF, _spec(mcq=2, long_answer=1))
    assert bundle.sources == ["src-section"]


def test_no_grounding_yields_an_empty_bundle():
    assert AssessmentGrounder(None).ground(_BRIEF, _spec(mcq=1)).is_empty


def test_bundle_merge_dedupes_chunks_and_sources():
    c = RetrievedChunk(text="same", score=1.0, payload={})
    a = GroundingBundle(chunks={"reference": [c]}, sources=["s1"])
    b = GroundingBundle(chunks={"reference": [c]}, sources=["s1", "s2"])
    m = a.merge(b)
    assert len(m.chunks["reference"]) == 1 and m.sources == ["s1", "s2"]
