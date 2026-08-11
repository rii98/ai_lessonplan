"""The eval harness — score lessons and turn the aggregate into a pass/fail gate."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..domain.ldd import IntakeRequest, LessonDesignDocument
from ..services.critique import Critic
from .scoring import structural_score


@dataclass(slots=True)
class CaseResult:
    label: str
    structural: float
    rubric_overall: float
    overall: float
    passed: bool
    rubric: dict[str, float] = field(default_factory=dict)
    structural_breakdown: dict[str, float] = field(default_factory=dict)


@dataclass(slots=True)
class EvalReport:
    cases: list[CaseResult]
    gate: float

    @property
    def mean_overall(self) -> float:
        return sum(c.overall for c in self.cases) / len(self.cases) if self.cases else 0.0

    @property
    def mean_structural(self) -> float:
        return sum(c.structural for c in self.cases) / len(self.cases) if self.cases else 0.0

    @property
    def mean_rubric(self) -> float:
        return sum(c.rubric_overall for c in self.cases) / len(self.cases) if self.cases else 0.0

    @property
    def pass_rate(self) -> float:
        return sum(c.passed for c in self.cases) / len(self.cases) if self.cases else 0.0

    @property
    def passed(self) -> bool:
        """The gate: the mean overall score clears the bar and every case is
        individually above a floor (half the gate) — so one great lesson can't
        mask one broken one."""
        floor = self.gate / 2.0
        return bool(self.cases) and self.mean_overall >= self.gate and all(
            c.overall >= floor for c in self.cases
        )

    def summary(self) -> str:
        verdict = "PASS" if self.passed else "FAIL"
        lines = [
            f"Eval: {len(self.cases)} case(s) · gate {self.gate:.2f} · {verdict}",
            (
                f"  mean overall={self.mean_overall:.3f}  "
                f"structural={self.mean_structural:.3f}  rubric={self.mean_rubric:.3f}  "
                f"pass_rate={self.pass_rate:.0%}"
            ),
        ]
        for c in self.cases:
            flag = "ok " if c.passed else "LOW"
            lines.append(
                f"  [{flag}] {c.overall:.3f}  "
                f"(struct {c.structural:.2f} / rubric {c.rubric_overall:.2f})  {c.label}"
            )
        return "\n".join(lines)


class EvalHarness:
    """Scores LDDs = ``structural_weight`` · structural + rest · rubric(critic)."""

    def __init__(
        self, *, critic: Critic, gate: float = 0.7, structural_weight: float = 0.4
    ) -> None:
        self.critic = critic
        self.gate = gate
        self.structural_weight = structural_weight

    def score_ldd(self, ldd: LessonDesignDocument, *, label: str = "") -> CaseResult:
        structural, breakdown = structural_score(ldd)
        critique = self.critic.critique(ldd)
        rubric_overall = critique.overall
        overall = (
            self.structural_weight * structural
            + (1.0 - self.structural_weight) * rubric_overall
        )
        return CaseResult(
            label=label or ldd.topic,
            structural=structural,
            rubric_overall=rubric_overall,
            overall=overall,
            passed=overall >= self.gate,
            rubric=critique.scores.as_dict(),
            structural_breakdown=breakdown,
        )

    def score_many(self, ldds: list[LessonDesignDocument]) -> EvalReport:
        return EvalReport(
            cases=[self.score_ldd(ldd, label=ldd.topic) for ldd in ldds],
            gate=self.gate,
        )

    def run_live(self, requests: list[IntakeRequest], pipeline) -> EvalReport:
        """Generate a lesson per request through the pipeline, then score it.
        ``pipeline`` is duck-typed (anything with ``.run(IntakeRequest)``) so this
        module never imports the container/API."""
        cases: list[CaseResult] = []
        for req in requests:
            label = req.topic or (req.existing_plan or "")[:60]
            try:
                ldd = pipeline.run(req)
                cases.append(self.score_ldd(ldd, label=label))
            except Exception as exc:  # a failed generation is a zero, not a crash
                cases.append(
                    CaseResult(
                        label=f"{label} (FAILED: {exc})",
                        structural=0.0,
                        rubric_overall=0.0,
                        overall=0.0,
                        passed=False,
                    )
                )
        return EvalReport(cases=cases, gate=self.gate)
