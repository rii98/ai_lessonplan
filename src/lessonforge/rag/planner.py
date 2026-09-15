"""RetrievalPlanner — decide *how much* context a need requires before retrieving.

Different needs want different amounts of the book: a unit plan wants the whole
chapter (broad), a lesson section wants its section, a single quiz question wants
one chunk (narrow). Feeding a definition the whole chapter buries the signal;
feeding a unit planner one chunk starves the arc. The planner picks the
granularity per need so multi-granularity retrieval (see :class:`Retriever`) is
actually used well.

Three providers, same loose-coupling contract as the rest of the system:

- ``heuristic`` — free and deterministic: an explicit need *kind* wins, else the
  need text's own stems ("define…" → narrow, "explain…" → section, "plan the
  unit…" → broad). Most needs are obvious; spend no tokens on them.
- ``llm`` — ask the fast model to classify, degrading to the heuristic on any
  failure so a bad/empty model response never blocks retrieval.
- ``hybrid`` — the senior default: heuristic first, and an LLM call *only* when
  the heuristic can't decide (free text with no clear signal).

Plans are cached by (kind, need) so a 5-day unit build decides each recurring
need once, not once per day.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass

from ..config import PlannerConfig
from ..providers.base import LLMClient
from .retriever import Granularity

_GRANULARITIES: frozenset[str] = frozenset({"narrow", "section", "broad"})


@dataclass(slots=True)
class RetrievalPlan:
    """What the planner decided for one need."""

    granularity: Granularity
    reason: str = ""


# An explicit need *kind* is the strongest signal — a caller that knows it is
# building a unit plan says so, and the granularity is not guessed from text.
_KIND_GRANULARITY: dict[str, Granularity] = {
    "unit_plan": "broad",
    "unit": "broad",
    "overview": "broad",
    "syllabus": "broad",
    "lesson": "section",
    "procedure": "section",
    "concept": "section",
    "explanation": "section",
    "objective": "narrow",
    "quiz_item": "narrow",
    "formative_check": "narrow",
    "definition": "narrow",
    "fact": "narrow",
}

# Query-text stems, checked when there's no explicit kind. Ordered broad → section
# → narrow: a "plan the whole unit across 5 days" beats an incidental "explain".
_BROAD_RE = re.compile(
    r"\b(unit plan|whole unit|entire (?:unit|chapter)|across .* (?:days|lessons)|"
    r"multi-?day|\d+[- ]day|syllabus|overview of|scope and sequence)\b",
    re.IGNORECASE,
)
_SECTION_RE = re.compile(
    r"\b(explain|describe|how (?:do|does|to)|why|process|procedure|steps|"
    r"walk through|relationship|compare)\b",
    re.IGNORECASE,
)
_NARROW_RE = re.compile(
    r"\b(define|definition|what is|what are|name the|list the|state the|"
    r"one (?:fact|example)|single)\b",
    re.IGNORECASE,
)


class RetrievalPlanner(ABC):
    """Chooses a :class:`RetrievalPlan` for a need. Providers self-register; the
    planner is resolved by config, same as the query transformer."""

    @abstractmethod
    def plan(self, need: str, *, kind: str | None = None) -> RetrievalPlan:
        """Decide the granularity for ``need`` (a topic or question). ``kind``, when
        given, is an explicit need type (e.g. ``"unit_plan"``) that overrides text
        inspection."""

    @classmethod
    def from_config(
        cls, cfg: PlannerConfig, *, llm: LLMClient | None = None
    ) -> RetrievalPlanner:  # pragma: no cover
        raise NotImplementedError


_REGISTRY: dict[str, type[RetrievalPlanner]] = {}


def register_planner(name: str) -> Callable[[type[RetrievalPlanner]], type[RetrievalPlanner]]:
    def deco(cls: type[RetrievalPlanner]) -> type[RetrievalPlanner]:
        _REGISTRY[name] = cls
        return cls

    return deco


def build_retrieval_planner(
    cfg: PlannerConfig, *, llm: LLMClient | None = None
) -> RetrievalPlanner:
    try:
        cls = _REGISTRY[cfg.provider]
    except KeyError:
        available = ", ".join(sorted(_REGISTRY)) or "<none>"
        raise ValueError(
            f"Unknown planner provider {cfg.provider!r}. Available: {available}."
        ) from None
    planner = cls.from_config(cfg, llm=llm)
    return _CachingPlanner(planner) if cfg.cache else planner


def _coerce_granularity(value: str, default: Granularity) -> Granularity:
    v = value.strip().lower()
    return v if v in _GRANULARITIES else default  # type: ignore[return-value]


class _CachingPlanner(RetrievalPlanner):
    """Memoize plans by (kind, need) — a recurring need is decided once. Wraps any
    provider; transparent to callers."""

    def __init__(self, inner: RetrievalPlanner) -> None:
        self.inner = inner
        self._cache: dict[tuple[str | None, str], RetrievalPlan] = {}

    def plan(self, need: str, *, kind: str | None = None) -> RetrievalPlan:
        key = (kind, need)
        if key not in self._cache:
            self._cache[key] = self.inner.plan(need, kind=kind)
        return self._cache[key]


@register_planner("heuristic")
class HeuristicPlanner(RetrievalPlanner):
    """Free, deterministic granularity from the need kind or the query stems."""

    def __init__(self, default_granularity: Granularity = "narrow") -> None:
        self.default: Granularity = default_granularity

    @classmethod
    def from_config(cls, cfg, *, llm=None) -> HeuristicPlanner:
        return cls(_coerce_granularity(cfg.default_granularity, "narrow"))

    def plan(self, need: str, *, kind: str | None = None) -> RetrievalPlan:
        plan, _ = self.decide(need, kind)
        return plan

    def decide(self, need: str, kind: str | None) -> tuple[RetrievalPlan, bool]:
        """Return the plan and whether it was *confidently* matched — the second
        value lets the hybrid planner know when to escalate to the LLM."""
        if kind:
            g = _KIND_GRANULARITY.get(kind.strip().lower())
            if g is not None:
                return RetrievalPlan(g, reason=f"kind={kind}"), True
        if _BROAD_RE.search(need):
            return RetrievalPlan("broad", reason="broad query stem"), True
        if _SECTION_RE.search(need):
            return RetrievalPlan("section", reason="section query stem"), True
        if _NARROW_RE.search(need):
            return RetrievalPlan("narrow", reason="narrow query stem"), True
        return RetrievalPlan(self.default, reason="default (no signal)"), False


_LLM_SYSTEM = (
    "You decide how much reference material a task needs from a textbook. Answer "
    "with exactly one word: 'narrow' (one fact/definition), 'section' (a whole "
    "section — a procedure or explanation), or 'broad' (a whole chapter — a unit "
    "plan or overview). Output only the one word."
)


@register_planner("llm")
class LLMPlanner(RetrievalPlanner):
    """Ask the fast model to classify the need. Degrades to the heuristic on any
    failure (no LLM, error, or an unrecognized answer) so retrieval never blocks."""

    def __init__(self, llm: LLMClient | None, default_granularity: Granularity = "narrow") -> None:
        self.llm = llm
        self.fallback = HeuristicPlanner(default_granularity)

    @classmethod
    def from_config(cls, cfg, *, llm=None) -> LLMPlanner:
        return cls(llm, _coerce_granularity(cfg.default_granularity, "narrow"))

    def plan(self, need: str, *, kind: str | None = None) -> RetrievalPlan:
        if self.llm is None:
            return self.fallback.plan(need, kind=kind)
        prompt = f"Task: {need}\n" + (f"Task type: {kind}\n" if kind else "") + "Answer:"
        try:
            text = self.llm.complete(prompt, system=_LLM_SYSTEM, temperature=0.0).text
        except Exception:
            return self.fallback.plan(need, kind=kind)
        word = text.strip().split()[0].lower() if text.strip() else ""
        if word in _GRANULARITIES:
            return RetrievalPlan(word, reason="llm")  # type: ignore[arg-type]
        return self.fallback.plan(need, kind=kind)


@register_planner("hybrid")
class HybridPlanner(RetrievalPlanner):
    """Heuristic first; an LLM call ONLY when the heuristic can't decide. The
    senior default — free on the obvious majority, smart on the ambiguous rest."""

    def __init__(self, llm: LLMClient | None, default_granularity: Granularity = "narrow") -> None:
        self.heuristic = HeuristicPlanner(default_granularity)
        self.llm_planner = LLMPlanner(llm, default_granularity)
        self.has_llm = llm is not None

    @classmethod
    def from_config(cls, cfg, *, llm=None) -> HybridPlanner:
        return cls(llm, _coerce_granularity(cfg.default_granularity, "narrow"))

    def plan(self, need: str, *, kind: str | None = None) -> RetrievalPlan:
        plan, confident = self.heuristic.decide(need, kind)
        if confident or not self.has_llm:
            return plan
        return self.llm_planner.plan(need, kind=kind)
