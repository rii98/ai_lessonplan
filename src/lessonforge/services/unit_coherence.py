"""UnitCoherence — the deterministic arc-quality check over an assembled unit.

Per-day quality is already guaranteed by the LDD's own guardrails; this pass looks
at what only shows up *across* days:

- **duplicate_topic** — two days teaching the same thing;
- **repeated_hook**   — the same engagement hook reused (a stale unit);
- **minute_budget**   — a day whose phase minutes stray far from its duration;
- **outcome_gap**     — a unit outcome no day's objectives address;
- **continuity**      — a day whose prior-knowledge doesn't connect to the day before.

It is a *report*, not a gate: text-matching heuristics are too soft to fail
construction on, but precise enough to flag for a teacher (and, later, to drive a
targeted single-day regeneration). All checks are lenient — biased toward silence,
so a flagged issue is worth a look.
"""

from __future__ import annotations

import re

from ..domain.ldd import LessonDesignDocument
from ..domain.unit import CoherenceIssue, CoherenceReport, UnitDesignDocument

_WORD_RE = re.compile(r"[a-z]{4,}")  # significant words only (drop short function words)


def _keywords(text: str) -> set[str]:
    return set(_WORD_RE.findall(text.lower()))


def _objective_keywords(ldd: LessonDesignDocument) -> set[str]:
    return _keywords(" ".join(o.statement for o in ldd.objectives))


class UnitCoherence:
    def __init__(self, *, minute_tolerance: float = 0.3, min_overlap: int = 1) -> None:
        # a day's phase minutes may stray this fraction from its duration before it
        # is flagged; ``min_overlap`` is how many shared keywords count as "connected".
        self.minute_tolerance = minute_tolerance
        self.min_overlap = min_overlap

    def apply(self, udd: UnitDesignDocument) -> UnitDesignDocument:
        """Return the unit with its coherence report attached."""
        return udd.model_copy(update={"coherence": self.assess(udd)})

    def assess(self, udd: UnitDesignDocument) -> CoherenceReport:
        issues: list[CoherenceIssue] = []
        issues += self._duplicate_topics(udd)
        issues += self._repeated_hooks(udd)
        issues += self._minute_budgets(udd)
        issues += self._outcome_gaps(udd)
        issues += self._continuity(udd)
        return CoherenceReport(ok=not issues, issues=issues)

    def _duplicate_topics(self, udd: UnitDesignDocument) -> list[CoherenceIssue]:
        seen: dict[str, int] = {}
        out: list[CoherenceIssue] = []
        for i, day in enumerate(udd.days, 1):
            key = day.topic.strip().lower()
            if key in seen:
                out.append(CoherenceIssue(
                    kind="duplicate_topic", day=i,
                    detail=f"day {i} repeats the topic of day {seen[key]}: {day.topic!r}",
                ))
            else:
                seen[key] = i
        return out

    def _repeated_hooks(self, udd: UnitDesignDocument) -> list[CoherenceIssue]:
        seen: dict[str, int] = {}
        out: list[CoherenceIssue] = []
        for i, day in enumerate(udd.days, 1):
            key = day.engagement_hook.prompt.strip().lower()
            if key in seen:
                out.append(CoherenceIssue(
                    kind="repeated_hook", day=i,
                    detail=f"day {i} reuses day {seen[key]}'s engagement hook",
                ))
            else:
                seen[key] = i
        return out

    def _minute_budgets(self, udd: UnitDesignDocument) -> list[CoherenceIssue]:
        out: list[CoherenceIssue] = []
        for i, day in enumerate(udd.days, 1):
            total = sum(p.minutes for p in day.phases)
            budget = day.duration_min
            if budget and abs(total - budget) > self.minute_tolerance * budget:
                out.append(CoherenceIssue(
                    kind="minute_budget", day=i,
                    detail=f"day {i} phases total {total} min vs. a {budget}-min lesson",
                ))
        return out

    def _outcome_gaps(self, udd: UnitDesignDocument) -> list[CoherenceIssue]:
        covered = set().union(*(_objective_keywords(d) for d in udd.days)) if udd.days else set()
        out: list[CoherenceIssue] = []
        for outcome in udd.unit_outcomes:
            kw = _keywords(outcome)
            if kw and len(kw & covered) < self.min_overlap:
                out.append(CoherenceIssue(
                    kind="outcome_gap", day=None,
                    detail=f"no day's objectives address the unit outcome: {outcome!r}",
                ))
        return out

    def _continuity(self, udd: UnitDesignDocument) -> list[CoherenceIssue]:
        out: list[CoherenceIssue] = []
        for i in range(1, len(udd.days)):
            prev_obj = _objective_keywords(udd.days[i - 1])
            prior_kw = _keywords(" ".join(udd.days[i].prior_knowledge))
            if prior_kw and prev_obj and len(prior_kw & prev_obj) < self.min_overlap:
                out.append(CoherenceIssue(
                    kind="continuity", day=i + 1,
                    detail=f"day {i + 1}'s prior knowledge doesn't connect to day {i}",
                ))
        return out
