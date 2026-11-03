"""LearnerModel — turns an observation into updated objective state. Code only.

The LLM never writes a mastery number (docs D7): LLM knowledge tracers update
inconsistently, sometimes in the wrong direction, while Bayesian knowledge tracing is
proven, cheap and testable. Evidence is weighted by how much we trust it: a graded
widget is strong; the tracer's reading of free text counts ``dialogue_weight``.

This is also where **early credit** happens (docs §5.1): coverage the tracer finds for
*another* objective's expectations is recorded on that objective (``early_evidence``),
without moving the cursor.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable

from ...config import LearnLearnerConfig
from ...domain.learning import (
    Act,
    Affect,
    Correctness,
    LearningSession,
    ObjectiveState,
    Observation,
    kc_key,
)

COVERED = 0.6   # an expectation counts as covered at or above this score


def bkt_update(p: float, correct: bool, cfg: LearnLearnerConfig, weight: float = 1.0) -> float:
    """One BKT step (posterior given the evidence, then the learning transition).
    ``weight`` < 1 blends the update toward the prior for weak evidence."""
    slip, guess = cfg.p_slip, cfg.p_guess
    if correct:
        post = p * (1 - slip) / (p * (1 - slip) + (1 - p) * guess)
    else:
        post = p * slip / (p * slip + (1 - p) * (1 - guess))
    post = post + (1 - post) * cfg.p_learn
    blended = p + weight * (post - p)
    return min(0.99, max(0.01, blended))


def uncovered(session: LearningSession, oid: str | None = None) -> list[str]:
    obj = session.plan.objective(oid or session.cursor)
    st = session.states[obj.id]
    return [e.id for e in obj.expectations if st.coverage.get(e.id, 0.0) < COVERED]


class LearnerModel(ABC):
    @abstractmethod
    def apply(self, session: LearningSession, obs: Observation) -> None:
        """Update ``session`` in place from one observation."""

    @abstractmethod
    def seed(self, session: LearningSession, prior: dict[str, float]) -> None:
        """Seed objective mastery from long-term records (``kc_key`` → p_mastery)."""


class BKTLearnerModel(LearnerModel):
    def __init__(self, cfg: LearnLearnerConfig) -> None:
        self.cfg = cfg

    def seed(self, session: LearningSession, prior: dict[str, float]) -> None:
        for o in session.plan.objectives:
            p = prior.get(kc_key(o, session.plan.topic))
            session.states[o.id].p_mastery = p if p is not None else self.cfg.p_init

    def apply(self, session: LearningSession, obs: Observation) -> None:
        session.turn_count += 1
        if obs.widget is not None:
            self._widget(session, obs)
        elif obs.trace is not None:
            self._trace(session, obs)

    # ── graded widget: strong evidence ───────────────────────────────────────
    def _widget(self, session: LearningSession, obs: Observation) -> None:
        w = obs.widget
        assert w is not None
        st = session.states[w.objective_id]
        obj = session.plan.objective(w.objective_id)
        if w.check_id not in st.used_checks:
            st.used_checks.append(w.check_id)
        if w.correct is None:          # self-check written answer: neutral
            return
        ok = bool(w.correct)
        st.p_mastery = bkt_update(st.p_mastery, ok, self.cfg)
        found = session.find_check(w.check_id)
        targets = found[1].targets if found else []
        if ok:
            st.checks_passed += 1
            st.attempts = 0
            st.hint_level = max(0, st.hint_level - 1)       # fading
            for t in targets:
                if obj.expectation(t):
                    st.coverage[t] = max(st.coverage.get(t, 0.0), 1.0)
            session.correct_streak += 1
        else:
            st.attempts += 1
            session.correct_streak = 0
            if w.misconception and w.misconception not in st.misconceptions_seen:
                st.misconceptions_seen.append(w.misconception)
        session.low_effort_streak = 0

    # ── free text: weaker evidence ───────────────────────────────────────────
    def _trace(self, session: LearningSession, obs: Observation) -> None:
        t = obs.trace
        assert t is not None
        st = session.state
        session.low_effort_streak = session.low_effort_streak + 1 if t.low_effort else 0

        # coverage — for the active objective AND any other (early credit)
        for eid, score in t.covered.items():
            oid = eid.split(".", 1)[0]
            if oid not in session.states:
                continue
            other: ObjectiveState = session.states[oid]
            prev = other.coverage.get(eid, 0.0)
            if score > prev:
                other.coverage[eid] = min(1.0, score)
            if (oid != session.cursor and score >= COVERED and other.status == "pending"
                    and not other.early_evidence):
                other.early_evidence = True
                obs.early.append(oid)
                other.p_mastery = bkt_update(other.p_mastery, True, self.cfg,
                                             self.cfg.dialogue_weight)

        if t.misconception and t.misconception.split(".", 1)[0] == session.cursor \
                and t.misconception not in st.misconceptions_seen:
            st.misconceptions_seen.append(t.misconception)

        if t.act is not Act.answer or t.correctness is Correctness.na:
            return
        w = self.cfg.dialogue_weight
        if t.correctness is Correctness.correct:
            st.p_mastery = bkt_update(st.p_mastery, True, self.cfg, w)
            st.attempts = 0
            st.hint_level = max(0, st.hint_level - 1)
            session.correct_streak = session.correct_streak + 1 \
                if t.affect in (Affect.confident, Affect.neutral) else 0
        elif t.correctness is Correctness.partial:
            session.correct_streak = 0
        else:
            st.p_mastery = bkt_update(st.p_mastery, False, self.cfg, w)
            st.attempts += 1
            session.correct_streak = 0


LEARNER_REGISTRY: dict[str, Callable[[LearnLearnerConfig], LearnerModel]] = {
    "bkt": BKTLearnerModel,
}


def build_learner(cfg: LearnLearnerConfig) -> LearnerModel:
    try:
        return LEARNER_REGISTRY[cfg.provider](cfg)
    except KeyError:
        raise ValueError(f"unknown learner model {cfg.provider!r}; "
                         f"known: {sorted(LEARNER_REGISTRY)}") from None
