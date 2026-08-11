"""Evaluation harness — quality as a measured number, not a vibe.

"Well tested" for a stochastic system means deterministic seams **plus** an eval
harness (SRD §10). This package scores lessons on a fixed golden set — structural
checks + the rubric critic — and turns the aggregate into a pass/fail gate that
runs in CI as the acceptance gate for prompt/model/provider changes.

Two modes:
- deterministic (default): score authored exemplar LDDs with the structural
  critic. No LLM, no services — safe for CI, guards against scorer regressions.
- live (``--generate``): run the real pipeline over the golden topics and score
  the output. Gated behind live services, same as the contract tests.
"""

from __future__ import annotations

from .harness import CaseResult, EvalHarness, EvalReport
from .scoring import structural_score

__all__ = ["CaseResult", "EvalHarness", "EvalReport", "structural_score"]
