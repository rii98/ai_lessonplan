"""Retrieval strategy for assessments — *what to retrieve* depends on what is asked.

A one-size grounding pass (3 narrow chunks) is wrong for an assessment in both
directions:

- **Fact-style questions** (MCQ, true/false, fill-in, match, short answer) each need
  a *distinct, precise fact*. Narrow, reranked chunks are exactly right — but the
  number needed scales with the question count, or a 20-question quiz is written
  from three paragraphs and repeats itself.
- **Explanatory questions** (long answer, ordering) need a *whole coherent passage*:
  the full explanation, the complete sequence of a process. A narrow chunk gives half
  a process and the model invents the rest. Retrieve the whole *section*.
- **Numerical questions** need *worked examples and formulae*, which live in their own
  sub-headings — so the query is steered at them, and the section is retrieved.
- **A revision test** over a chapter wants breadth: ``scope="chapter"`` retrieves
  whole chapters (the author opts in; it is the most expensive context).

So :func:`plan_passes` turns a blueprint into a few small, named retrieval passes
(never more than one per profile present), and :class:`AssessmentGrounder` runs them
and merges the bundles. The plan is a pure function of the spec — cheap to test and to
preview in the UI before any LLM call is spent.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..domain.assessment import AssessmentSpec, RetrievalProfile, meta
from ..domain.ldd import NormalizedBrief
from ..rag.grounding import GroundingBundle, GroundingRetriever
from ..rag.retriever import Granularity

# Steers the query toward the part of a chapter that actually contains this kind of
# material (narrow facts need no steer: the topic itself is the query).
_QUERY_STEER: dict[RetrievalProfile, str] = {
    "facts": "",
    "explain": "explanation how why process steps stages",
    "worked": "worked example solved problem formula calculation",
}
_WHY: dict[RetrievalProfile, str] = {
    "facts": "precise facts for objective questions — narrow reranked chunks, sized to the question count",
    "explain": "a whole coherent passage so explanations and sequences are complete — full sections",
    "worked": "worked examples and formulae — full sections, query steered at solved problems",
}
_GRAN: dict[RetrievalProfile, Granularity] = {"facts": "narrow", "explain": "section", "worked": "section"}
_SCOPE_GRAN: dict[str, Granularity] = {"focused": "narrow", "section": "section", "chapter": "broad"}

FACT_TOP_N_MIN, FACT_TOP_N_MAX = 3, 8
WHOLE_TOP_N = 2  # section/chapter hits are large; a couple is plenty


@dataclass(frozen=True, slots=True)
class RetrievalPass:
    profile: RetrievalProfile
    granularity: Granularity
    top_n: int
    query_steer: str
    why: str

    def describe(self) -> dict[str, object]:
        return {"profile": self.profile, "granularity": self.granularity,
                "top_n": self.top_n, "why": self.why}


def plan_passes(spec: AssessmentSpec) -> list[RetrievalPass]:
    """The retrieval passes for ``spec`` — one per retrieval profile it needs.

    ``scope="auto"`` picks per profile (see module docstring). Any other scope
    forces that granularity for every pass (and a single ``chapter`` pass replaces
    them all, since a whole chapter already contains every kind of material)."""
    count_by_profile: dict[RetrievalProfile, int] = {}
    for row in spec.types:
        p = meta(row.type).retrieval
        count_by_profile[p] = count_by_profile.get(p, 0) + row.count

    if spec.scope == "chapter":
        return [RetrievalPass(
            "explain", "broad", WHOLE_TOP_N, "",
            "revision scope — whole chapters so questions can range across the chapter")]

    passes: list[RetrievalPass] = []
    for profile in ("facts", "explain", "worked"):  # stable order
        if profile not in count_by_profile:
            continue
        gran = _SCOPE_GRAN.get(spec.scope) or _GRAN[profile]
        if gran == "narrow":
            n = count_by_profile[profile]
            top_n = min(FACT_TOP_N_MAX, max(FACT_TOP_N_MIN, math.ceil(n / 2) + 1))
        else:
            top_n = WHOLE_TOP_N
        passes.append(RetrievalPass(profile, gran, top_n, _QUERY_STEER[profile], _WHY[profile]))
    return passes


class AssessmentGrounder:
    """Runs the retrieval plan and merges the passes into one bundle."""

    def __init__(self, grounding: GroundingRetriever | None) -> None:
        self.grounding = grounding

    def ground(self, brief: NormalizedBrief, spec: AssessmentSpec) -> GroundingBundle:
        if self.grounding is None:
            return GroundingBundle()
        base = f"{brief.topic} grade {brief.grade} {brief.subject}"
        merged = GroundingBundle()
        for p in plan_passes(spec):
            merged = merged.merge(self._run(brief, f"{base} {p.query_steer}".strip(), p))
        return merged

    def _run(self, brief: NormalizedBrief, query: str, p: RetrievalPass) -> GroundingBundle:
        try:
            return self.grounding.ground(  # type: ignore[union-attr]
                query=query, grade=brief.grade, subject=brief.subject,
                framework=brief.framework, granularity=p.granularity, top_n=p.top_n,
            )
        except Exception:
            return GroundingBundle()  # one failed pass must not sink the others
