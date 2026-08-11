"""The eval harness: structural scoring, the gate, and the CLI."""

from __future__ import annotations

from lessonforge.eval import EvalHarness, structural_score
from lessonforge.eval.__main__ import main
from lessonforge.services.critique import StructuralCritic


def test_structural_score_rewards_a_complete_lesson(export_ldd):
    score, breakdown = structural_score(export_ldd)
    assert score > 0.9  # balanced timing, differentiation, homework, checks
    assert breakdown["timing_balance"] == 1.0  # phases sum to 45


def test_structural_score_penalizes_a_thin_lesson(weak_ldd):
    score, breakdown = structural_score(weak_ldd)
    assert score < 0.7
    assert breakdown["differentiation"] == 0.0
    assert breakdown["homework"] == 0.0


def test_harness_scores_and_gates(export_ldd, weak_ldd):
    harness = EvalHarness(critic=StructuralCritic(), gate=0.7)
    report = harness.score_many([export_ldd, weak_ldd])
    assert len(report.cases) == 2
    # one strong, one weak → aggregate drops below the gate and one case is LOW
    assert report.passed is False
    assert any(not c.passed for c in report.cases)
    assert "FAIL" in report.summary()


def test_harness_passes_on_all_strong(export_ldd):
    report = EvalHarness(critic=StructuralCritic(), gate=0.7).score_many([export_ldd])
    assert report.passed is True
    assert report.mean_overall >= 0.7


def test_run_live_scores_generated_lessons(export_ldd):
    class FakePipeline:
        def run(self, request):
            return export_ldd

    from lessonforge.domain.ldd import IntakeRequest

    report = EvalHarness(critic=StructuralCritic(), gate=0.7).run_live(
        [IntakeRequest(topic="X", grade=6, subject="Science")], FakePipeline()
    )
    assert report.cases[0].passed


def test_run_live_records_a_failed_generation():
    class BoomPipeline:
        def run(self, request):
            raise RuntimeError("model down")

    from lessonforge.domain.ldd import IntakeRequest

    report = EvalHarness(critic=StructuralCritic(), gate=0.7).run_live(
        [IntakeRequest(topic="X", grade=6, subject="Science")], BoomPipeline()
    )
    assert report.cases[0].overall == 0.0 and not report.cases[0].passed


def test_cli_default_gate_passes_on_shipped_exemplars():
    assert main([]) == 0  # deterministic gate over corpus/golden/exemplar_ldds.jsonl


def test_cli_fails_when_gate_is_unreachable():
    assert main(["--gate", "0.999"]) == 1
