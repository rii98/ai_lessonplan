"""The revise loop — critique an LDD, rewrite weak sections, repeat to threshold.

Given a draft LDD, the reviser scores it with the injected :class:`Critic`. While
the weighted overall is below ``threshold`` and the iteration budget remains, it
asks the LLM to rewrite the weak sections (keeping everything else), re-validates
the result against the LDD schema, and re-scores. A rewrite is kept only if it
actually improves the overall score; otherwise the loop stops and the best draft
so far is returned. Either way the final rubric scores are stamped into
``ldd.quality`` so downstream consumers see a measured number, not a vibe.

Reliability (SRD §09): every failure path degrades rather than crashes — a bad
rewrite (non-JSON, schema-invalid, or no improvement) simply keeps the current
best. With no LLM, or a critique that already passes, ``revise`` is a no-op that
still stamps the scores. This makes the loop a clean deterministic seam: with the
default structural critic and no LLM, the output is a pure function of the input.
"""

from __future__ import annotations

from typing import Any

from ..config import CritiqueConfig
from ..domain.ldd import LessonDesignDocument, NormalizedBrief
from ..domain.rubric import DIMENSION_SECTIONS, Critique
from ..providers.base import LLMClient
from ..util import extract_json
from .critique import Critic

_REVISE_SYSTEM = (
    "You are an experienced Nepali teacher revising a lesson plan. You improve ONLY "
    "the weak sections you are told to, preserving the rest verbatim, and you keep "
    "the exact same JSON structure. You output ONLY valid JSON."
)

_REVISE_PROMPT = """\
This lesson plan scored below the quality bar. Rewrite it to fix the weak sections
listed, keeping every other field unchanged and keeping the EXACT same JSON schema.

Weak sections and how to fix them:
{weaknesses}

Current lesson (JSON):
{ldd}

Return the full improved lesson as a single JSON object with the same keys and
nesting. Keep objective ids stable and keep every objective both taught (in a
phase) and assessed (in a formative check). Return JSON only, no prose.
"""


class Reviser:
    def __init__(
        self,
        *,
        llm: LLMClient | None,
        critic: Critic,
        threshold: float = 0.7,
        max_iterations: int = 1,
    ) -> None:
        self.llm = llm
        self.critic = critic
        self.threshold = threshold
        self.max_iterations = max(0, max_iterations)

    @classmethod
    def from_config(
        cls, cfg: CritiqueConfig, *, llm: LLMClient | None, critic: Critic
    ) -> Reviser:
        return cls(
            llm=llm,
            critic=critic,
            threshold=cfg.threshold,
            max_iterations=cfg.max_iterations,
        )

    def revise(
        self, ldd: LessonDesignDocument, brief: NormalizedBrief | None = None
    ) -> LessonDesignDocument:
        best = ldd
        best_critique = self.critic.critique(best)
        accepted = 0
        for _ in range(self.max_iterations):
            if best_critique.overall >= self.threshold or self.llm is None:
                break
            candidate = self._rewrite(best, best_critique)
            if candidate is None:
                break  # rewrite failed (non-JSON / schema-invalid) → keep best
            cand_critique = self.critic.critique(candidate)
            if cand_critique.overall <= best_critique.overall:
                break  # no improvement → stop spending tokens
            best, best_critique = candidate, cand_critique
            accepted += 1

        self._stamp(best, best_critique, accepted)
        return best

    def _rewrite(
        self, ldd: LessonDesignDocument, critique: Critique
    ) -> LessonDesignDocument | None:
        weak = critique.scores.weak_dimensions(self.threshold)
        if not weak:
            return None
        weaknesses = "\n".join(
            f"- {DIMENSION_SECTIONS[dim]}: "
            f"{critique.suggestions.get(DIMENSION_SECTIONS[dim], 'strengthen this section')}"
            for dim in weak
        )
        prompt = _REVISE_PROMPT.format(
            weaknesses=weaknesses,
            ldd=ldd.model_dump_json(indent=2),
        )
        try:
            result = self.llm.complete(  # type: ignore[union-attr]
                prompt,
                system=_REVISE_SYSTEM,
                json_schema=LessonDesignDocument.model_json_schema(),
                temperature=0.4,
            )
            data: dict[str, Any] = extract_json(result.text)
            candidate = LessonDesignDocument.model_validate(data)
        except Exception:
            # non-JSON, schema-invalid, or a network hiccup → keep the current best
            return None
        # provenance is authoritative — the rewrite can't invent new citations,
        # and it carries the generation-time adjustment trail forward intact
        candidate.quality.grounding_sources = ldd.quality.grounding_sources
        candidate.quality.adjustments = ldd.quality.adjustments
        return candidate

    def _stamp(
        self, ldd: LessonDesignDocument, critique: Critique, iterations: int
    ) -> None:
        q = ldd.quality
        s = critique.scores
        q.engagement = s.engagement
        q.alignment = s.alignment
        q.misconception_coverage = s.misconception_coverage
        q.specificity = s.specificity
        q.local_relevance = s.local_relevance
        passed = critique.overall >= self.threshold
        q.notes = (
            f"critique overall={critique.overall:.2f} "
            f"(threshold {self.threshold:.2f}, {'passed' if passed else 'below bar'}); "
            f"{iterations} revision(s); {critique.notes}"
        )
