"""Eval CLI — the CI acceptance gate.

    # deterministic: score the authored exemplar LDDs (no LLM, no services)
    python -m lessonforge.eval

    # live: generate from the golden topics through the real pipeline, then score
    RUN_INTEGRATION=1 python -m lessonforge.eval --generate

Exit code is 0 when the aggregate clears the gate, 1 otherwise — so CI fails on
silent quality drift after a prompt or model change.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ..domain.ldd import IntakeRequest, LessonDesignDocument
from .harness import EvalHarness

_GOLDEN_DIR = Path(__file__).resolve().parents[3] / "corpus" / "golden"
_DEFAULT_FIXTURES = _GOLDEN_DIR / "exemplar_ldds.jsonl"
_DEFAULT_TOPICS = _GOLDEN_DIR / "topics.jsonl"


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        raise SystemExit(f"eval: file not found: {path}")
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def _run_fixtures(fixtures: Path, gate: float) -> int:
    """Deterministic gate: structural critic over authored exemplar LDDs."""
    from ..services.critique import StructuralCritic

    ldds = [LessonDesignDocument.model_validate(r) for r in _load_jsonl(fixtures)]
    harness = EvalHarness(critic=StructuralCritic(), gate=gate)
    report = harness.score_many(ldds)
    print(report.summary())
    return 0 if report.passed else 1


def _run_live(topics: Path, gate: float) -> int:
    """Live gate: generate through the real pipeline, then score with the
    configured critic."""
    from ..container import Container

    container = Container.from_settings()
    requests = [IntakeRequest(**r) for r in _load_jsonl(topics)]
    harness = EvalHarness(critic=container.critic, gate=gate)
    report = harness.run_live(requests, container.pipeline)
    print(report.summary())
    return 0 if report.passed else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lessonforge.eval", description=__doc__)
    parser.add_argument("--gate", type=float, default=0.7, help="pass threshold (default 0.7)")
    parser.add_argument(
        "--generate",
        action="store_true",
        help="live mode: generate from the golden topics through the pipeline (needs services)",
    )
    parser.add_argument("--fixtures", type=Path, default=_DEFAULT_FIXTURES)
    parser.add_argument("--topics", type=Path, default=_DEFAULT_TOPICS)
    args = parser.parse_args(argv)

    if args.generate:
        return _run_live(args.topics, args.gate)
    return _run_fixtures(args.fixtures, args.gate)


if __name__ == "__main__":
    sys.exit(main())
