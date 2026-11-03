"""PedagogicalPolicy — decide the next teaching move. Code, not the LLM.

StratL showed a hand-built strategy beats letting the LLM choose; Bridge showed that
telling the model the error and the strategy improves its reply by 75%. So the
policy picks ONE :class:`MoveKind` (plus which widget, which objective, how much a
hint may reveal) from the structured state; the renderer only phrases it.

The default strategy, ``scaffolded``, is AutoTutor's expectation/misconception loop
with a hint ladder and fading, plus the "keep it alive" rules (docs §5.6):
ask/tell balance, a novice path, widget rotation, quick actions, pace adaptation,
a hook per objective, choice at branch points, boredom detection, early credit, and
break offers. Every threshold is config (:class:`LearnPolicyConfig`).

The policy is a pure function of ``(session, observation)`` — it never mutates the
session; the service applies the :class:`Decision`. That is what makes every rule a
one-line table test.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable

from ...config import LearnPolicyConfig
from ...domain.learning import (
    Act,
    CheckItem,
    Correctness,
    Decision,
    LearningSession,
    MoveKind,
    Observation,
    QuickAction,
)
from ...util import overlap
from .learner import uncovered

# moves that end by asking the student something (ask/tell balance, W1)
ASKING = frozenset({MoveKind.elicit, MoveKind.pump, MoveKind.hint, MoveKind.confront,
                    MoveKind.hook, MoveKind.affirm_and_extend})


class PedagogicalPolicy(ABC):
    @abstractmethod
    def decide(self, session: LearningSession, obs: Observation) -> Decision:
        """The next move. Must not mutate ``session``."""

    @abstractmethod
    def opening(self, session: LearningSession) -> Decision:
        """The first move of a fresh session."""

    @abstractmethod
    def resume(self, session: LearningSession) -> Decision:
        """The first move after a paused session is resumed."""

    @abstractmethod
    def advance(self, session: LearningSession, *, notes: tuple[str, ...] = ()) -> Decision:
        """Move past the current objective (used when no new check can be made)."""


# ── shared helpers (also used by the service after an advance) ───────────────
def is_mastered(session: LearningSession, oid: str, threshold: float) -> bool:
    st = session.states[oid]
    return st.status in ("mastered", "skipped") or (st.p_mastery >= threshold and st.checks_passed >= 1)


def enough_evidence(session: LearningSession, oid: str, threshold: float) -> bool:
    """Time to move on: mastered by BKT, OR two checks passed, OR at least one passed and
    every check has been used. Without the last two, an objective whose few checks
    included a miss could never be finished — the tutor would cycle the same checks."""
    st = session.states[oid]
    if is_mastered(session, oid, threshold) and not uncovered(session, oid):
        return True
    return st.checks_passed >= 2 or (st.checks_passed >= 1 and not session.unused_checks(oid))


def pick_check(session: LearningSession, oid: str, *, purpose: str = "check",
               prefer: tuple[str, ...] = (), allow_used: bool = False) -> CheckItem | None:
    """An UNUSED check for ``oid``, rotating widget kinds (never the same kind twice in a
    row, W3); targets of still-uncovered expectations are preferred. A check is never
    shown twice in a lesson — once answered, its answer has been revealed, so it no
    longer measures anything. ``None`` when all are used (the caller moves on or asks
    for a fresh one). ``allow_used`` is for spaced warm-ups on a later day."""
    obj = session.plan.objective(oid)
    if purpose == "challenge":
        return obj.challenge
    st = session.states[oid]
    pool = obj.checks if allow_used else session.unused_checks(oid)
    if not pool:
        return None
    open_ids = set(uncovered(session, oid)) if obj.expectations else set()

    def rank(c: CheckItem) -> tuple:
        return (c.id in st.used_checks,                        # unseen first (warm-ups)
                c.widget == session.last_widget_kind,           # rotate kinds
                c.widget not in prefer,                         # a preferred kind (e.g. predict)
                not (set(c.targets) & open_ids))                # targets what's still open
    return min(pool, key=rank)


DONE = ("mastered", "skipped", "left")


def available_next(session: LearningSession, threshold: float) -> list[str]:
    """Objectives that could come next: not done, every prerequisite done. In plan order.
    A ``left`` objective counts as done for sequencing — revisited by spaced review, not
    re-offered in the same lesson (that would be a loop of its own)."""
    done = {o.id for o in session.plan.objectives
            if is_mastered(session, o.id, threshold) or session.states[o.id].status in DONE}
    return [o.id for o in session.plan.objectives
            if o.id not in done and o.id != session.cursor
            and all(p in done or p == session.cursor for p in o.prerequisites)]


class ScaffoldedPolicy(PedagogicalPolicy):
    def __init__(self, cfg: LearnPolicyConfig) -> None:
        self.cfg = cfg

    # ── entry points ─────────────────────────────────────────────────────────
    def opening(self, session):
        if session.state.p_mastery >= 0.7:   # long-term memory says they likely know it
            check = pick_check(session, session.cursor)
            if check is not None:
                return Decision(move=MoveKind.skip_check, objective_id=session.cursor,
                                check_id=check.id, check_purpose="skip", notes=["known_before"])
        return Decision(move=MoveKind.hook, objective_id=session.cursor)

    def resume(self, session):
        done = [o.id for o in session.plan.objectives
                if session.states[o.id].status == "mastered" and o.checks]
        if done:   # spaced retrieval practice on the last thing mastered
            check = pick_check(session, done[-1], allow_used=True)
            if check is not None:
                return Decision(move=MoveKind.warm_up, objective_id=done[-1],
                                check_id=check.id, check_purpose="warm_up")
        return Decision(move=MoveKind.elicit, objective_id=session.cursor)

    def advance(self, session, *, notes=()):
        return self._advance(session, notes=notes)

    def decide(self, session, obs):
        if obs.action is not None:
            d = self._action(session, obs)
        elif obs.widget is not None:
            d = self._widget(session, obs)
        elif obs.trace is not None:
            d = self._text(session, obs)
        else:
            d = Decision(move=MoveKind.elicit, objective_id=session.cursor)
        d = self._rhythm(session, obs, d)
        # loop breaker: whatever happened, never keep one objective going forever —
        # give the key idea, move on, and let spaced review bring it back
        if (session.state.turns >= self.cfg.max_turns_per_objective and not d.end_status
                and not d.advance_to and not d.choices and obs.action is not QuickAction.resume):
            return self._advance(session, feedback=d.feedback, notes=("moving_on",))
        return d

    # ── quick actions (W4): deterministic ────────────────────────────────────
    def _action(self, s: LearningSession, obs: Observation) -> Decision:
        a, cur = obs.action, s.cursor
        st = s.state
        if a is QuickAction.pause:
            return Decision(move=MoveKind.pause, objective_id=cur, end_status="paused")
        if a is QuickAction.end:
            return Decision(move=MoveKind.wrap_up, objective_id=cur, end_status="ended")
        if a is QuickAction.resume:
            return self.resume(s)
        if a is QuickAction.hint:
            return self._hint(s, st.hint_level + 1)
        if a is QuickAction.example:
            return Decision(move=MoveKind.worked_step, objective_id=cur)
        if a is QuickAction.skip:
            return self._skip(s)
        if a is QuickAction.faster:
            if enough_evidence(s, cur, self.cfg.mastered):
                return self._advance(s).model_copy(update={"pace": "fast"})
            return self._check(s, MoveKind.pace_change).model_copy(update={"pace": "fast"})
        if a is QuickAction.slower:
            return Decision(move=MoveKind.explain_chunk, objective_id=cur, pace="slow")
        if a is QuickAction.choose and obs.action_value in s.awaiting_choice:
            return Decision(move=MoveKind.hook, objective_id=obs.action_value,
                            advance_to=obs.action_value,
                            check_next=s.states[obs.action_value].early_evidence)
        return Decision(move=MoveKind.elicit, objective_id=cur)

    # ── a graded widget answer ───────────────────────────────────────────────
    def _widget(self, s: LearningSession, obs: Observation) -> Decision:
        w = obs.widget
        assert w is not None
        cur = s.cursor
        if w.purpose == "warm_up":       # back to where we were, commenting on the warm-up
            return Decision(move=MoveKind.elicit, objective_id=cur, feedback=w)
        if w.purpose == "challenge":
            return self._advance(s, feedback=w)
        if w.purpose == "skip":          # verifying prior knowledge ("I know this", early credit)
            if w.correct:
                return self._advance(s, feedback=w)
            return Decision(move=MoveKind.explain_chunk, objective_id=cur, feedback=w)
        st = s.states[w.objective_id]
        if w.correct is None:            # a self-marked written answer: carry on
            return Decision(move=MoveKind.affirm_and_extend, objective_id=cur, feedback=w)
        if w.correct:
            if enough_evidence(s, cur, self.cfg.mastered):
                if s.pace == "fast" and s.objective.challenge is not None \
                        and s.objective.challenge.id not in st.used_checks:
                    return Decision(move=MoveKind.challenge, objective_id=cur, feedback=w,
                                    check_id=s.objective.challenge.id, check_purpose="challenge")
                return self._advance(s, feedback=w)
            if uncovered(s):
                return Decision(move=MoveKind.affirm_and_extend, objective_id=cur, feedback=w)
            return self._check(s, MoveKind.check, feedback=w)
        # wrong
        if w.misconception:
            return Decision(move=MoveKind.confront, objective_id=cur, feedback=w,
                            notes=[f"misconception:{w.misconception}"])
        if st.attempts > self.cfg.max_hints:
            return self._bottom_out(s, feedback=w)
        return Decision(move=MoveKind.reexplain, objective_id=cur, feedback=w,
                        hint_level=st.hint_level + 1)

    # ── free text ────────────────────────────────────────────────────────────
    def _text(self, s: LearningSession, obs: Observation) -> Decision:
        t = obs.trace
        assert t is not None
        cur, st, cfg = s.cursor, s.state, self.cfg
        notes = [f"noticed_ahead:{oid}" for oid in obs.early]

        if t.act is Act.stop_request:
            return Decision(move=MoveKind.wrap_up, objective_id=cur, end_status="ended")
        if t.act is Act.switch_topic and t.topic:
            return Decision(move=MoveKind.pause, objective_id=cur, end_status="paused",
                            switch_topic=t.topic)
        if t.act is Act.skip_request:
            if t.move_on:               # the student's call — honoured, recorded as "left"
                return self._advance(s, notes=("student_moved_on",))
            return self._skip(s)
        if s.awaiting_choice and t.act in (Act.answer, Act.chitchat):
            # a typed choice ("chlorophyll") → the offered objective it names best
            best = max(s.awaiting_choice,
                       key=lambda oid: overlap(s.plan.objective(oid).title, obs.text))
            return Decision(move=MoveKind.hook, objective_id=best, advance_to=best,
                            check_next=s.states[best].early_evidence)
        if t.act is Act.help_request:
            if t.wants_answer:
                if st.hint_level == 0 and st.attempts == 0 and not st.explained:
                    return self._hint(s, 1, notes=[*notes, "one_nudge"])
                return self._bottom_out(s, notes=notes)
            if not st.explained and st.attempts <= 1:            # novice path (W2)
                return Decision(move=MoveKind.explain_chunk, objective_id=cur, notes=notes)
            return self._hint(s, st.hint_level + 1, notes=notes)
        if t.act is Act.question_off_topic:
            return Decision(move=MoveKind.park, objective_id=cur, notes=[*notes, "park"])
        if t.act is Act.question_in_scope:
            steer = st.tangents >= cfg.tangent_budget
            return Decision(move=MoveKind.answer_tangent, objective_id=cur,
                            retrieve="coverage_gap",
                            notes=[*notes, *(["steer_back"] if steer else [])])
        if t.act is Act.chitchat:
            if s.low_effort_streak >= cfg.low_effort_streak:      # boredom (W10)
                check = pick_check(s, cur, prefer=("predict", "order", "match"))
                if check is not None:
                    return Decision(move=MoveKind.check, objective_id=cur, check_id=check.id,
                                    notes=[*notes, "switch_it_up"])
            return Decision(move=MoveKind.elicit, objective_id=cur, notes=notes)

        # an answer
        if t.misconception and t.misconception.startswith(f"{cur}.") \
                and st.misconceptions_seen.count(t.misconception) <= 1:
            return Decision(move=MoveKind.confront, objective_id=cur,
                            notes=[*notes, f"misconception:{t.misconception}"])
        if t.correctness is Correctness.correct:
            fast = s.pace == "fast" or s.correct_streak >= cfg.fast_track_streak
            # all ideas shown → check; fast learners check early, but only with a check left
            if not uncovered(s) or (fast and s.unused_checks(cur)):
                return self._check(s, MoveKind.check, notes=(
                    *notes, *(["fast_track"] if fast and uncovered(s) else [])))
            return Decision(move=MoveKind.affirm_and_extend, objective_id=cur, notes=notes)
        if t.correctness is Correctness.partial:
            return Decision(move=MoveKind.pump, objective_id=cur, notes=notes)
        # incorrect
        if st.attempts > cfg.max_hints:
            if not st.explained:
                return Decision(move=MoveKind.worked_step, objective_id=cur, notes=notes)
            return self._bottom_out(s, notes=notes)
        return self._hint(s, max(st.hint_level + 1, st.attempts), notes=notes)

    # ── building blocks ──────────────────────────────────────────────────────
    def _hint(self, s: LearningSession, level: int, notes: list[str] | None = None) -> Decision:
        if level > self.cfg.max_hints:
            if not s.state.explained:
                return Decision(move=MoveKind.worked_step, objective_id=s.cursor, notes=notes or [])
            return self._bottom_out(s, notes=notes)
        return Decision(move=MoveKind.hint, objective_id=s.cursor, hint_level=level,
                        notes=notes or [])

    def _check(self, s: LearningSession, move: MoveKind, *, feedback=None, notes=(),
               purpose: str = "check") -> Decision:
        """Show an unused check. If every check is used: enough evidence → move on;
        otherwise ask for a fresh one (never re-show an answered check)."""
        check = pick_check(s, s.cursor)
        if check is not None:
            return Decision(move=move, objective_id=s.cursor, check_id=check.id,
                            check_purpose=purpose, feedback=feedback, notes=list(notes))
        if s.state.checks_passed >= 1:
            return self._advance(s, feedback=feedback, notes=("checks_exhausted",))
        return Decision(move=move, objective_id=s.cursor, fresh_check=True,
                        check_purpose=purpose, feedback=feedback, notes=list(notes))

    def _bottom_out(self, s: LearningSession, *, feedback=None, notes=None) -> Decision:
        """Give the answer with its reasoning — then a transfer check (Bastani: the
        answer is fine; the crutch is the problem)."""
        check = pick_check(s, s.cursor)
        return Decision(move=MoveKind.bottom_out, objective_id=s.cursor, feedback=feedback,
                        check_id=check.id if check else None, fresh_check=check is None,
                        notes=notes or [])

    def _skip(self, s: LearningSession) -> Decision:
        """"I know this": already shown with a passed check → just move on; else verify."""
        if s.state.checks_passed >= 1:
            return self._advance(s)
        return self._check(s, MoveKind.skip_check, purpose="skip")

    def _advance(self, s: LearningSession, *, feedback=None, notes=()) -> Decision:
        nxt = available_next(s, self.cfg.mastered)
        if not nxt:
            return Decision(move=MoveKind.wrap_up, objective_id=s.cursor, feedback=feedback,
                            end_status="completed", notes=list(notes))
        early = [o for o in nxt if s.states[o].early_evidence]
        # every objective in ``nxt`` has its prerequisites met, so any two are a real
        # choice — let the student pick at a branch (W9)
        if not early and len(nxt) >= 2 and not s.choice_offered:
            return Decision(move=MoveKind.choose_next, objective_id=s.cursor, feedback=feedback,
                            choices=nxt[:2], notes=[*notes, "offer_break"])
        target = early[0] if early else nxt[0]
        return Decision(move=MoveKind.recap_advance, objective_id=s.cursor, feedback=feedback,
                        advance_to=target, check_next=s.states[target].early_evidence,
                        notes=[*notes, "offer_break"])

    # ── rhythm rules applied to every decision (W1, cadence) ─────────────────
    def _rhythm(self, s: LearningSession, obs: Observation, d: Decision) -> Decision:
        if d.end_status or d.advance_to or d.check_id or d.choices or d.fresh_check:
            return d
        cfg = self.cfg
        # ask/tell balance: never more than N questions in a row without giving something
        if d.move in (MoveKind.elicit, MoveKind.pump) and s.questions_in_a_row >= cfg.max_questions_in_a_row:
            return d.model_copy(update={"move": MoveKind.explain_chunk,
                                        "notes": [*d.notes, "ask_tell_balance"]})
        # interaction cadence: a widget at least every N tutor turns
        if d.move in (MoveKind.elicit, MoveKind.affirm_and_extend, MoveKind.pump,
                      MoveKind.explain_chunk) and s.turns_since_widget + 1 >= cfg.widget_every:
            check = pick_check(s, s.cursor)
            if check is not None:
                return d.model_copy(update={"check_id": check.id, "notes": [*d.notes, "cadence"]})
        return d


POLICY_REGISTRY: dict[str, Callable[[LearnPolicyConfig], PedagogicalPolicy]] = {
    "scaffolded": ScaffoldedPolicy,
}


def build_policy(cfg: LearnPolicyConfig) -> PedagogicalPolicy:
    try:
        return POLICY_REGISTRY[cfg.provider](cfg)
    except KeyError:
        raise ValueError(f"unknown pedagogy policy {cfg.provider!r}; "
                         f"known: {sorted(POLICY_REGISTRY)}") from None
