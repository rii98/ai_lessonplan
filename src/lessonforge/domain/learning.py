"""Learn mode — the domain of a scaffolded tutoring session (docs/LEARN_MODE.md).

Pure data + validators, no I/O. Three groups:

- the **plan** a session teaches (:class:`LearningPlan` → :class:`Objective` →
  expectations / misconceptions / check items / pinned evidence) — compiled once from
  the corpus and then held fixed, which is what keeps a long dialogue on its goal;
- the **live state** (:class:`LearningSession`, :class:`ObjectiveState`) — structured and
  exact, never an LLM summary, so the next move is always decided from facts;
- the **per-turn vocabulary** (:class:`Observation`, :class:`Trace`, :class:`Decision`,
  :class:`MoveKind`) that flows tracer → learner model → policy → renderer.

The hidden fields (worked example, answers, hint ladder) never leave the server:
:meth:`LearningSession.public` is the only view the API returns.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from .ldd import Question


def _now() -> datetime:
    return datetime.now(UTC)


def _new_id() -> str:
    return str(uuid.uuid4())


# ── the plan ──────────────────────────────────────────────────────────────────
class PinnedChunk(BaseModel):
    """A retrieved passage the tutor is grounded in (provenance travels with it)."""

    text: str
    source: str = ""
    collection: str = ""
    score: float = 0.0
    metadata: dict[str, Any] = Field(default_factory=dict)


class Expectation(BaseModel):
    """An idea a complete answer must contain (AutoTutor's *expectation*)."""

    id: str                              # namespaced: "O1.e1"
    statement: str
    keywords: list[str] = Field(default_factory=list)   # cheap coverage signal


class Misconception(BaseModel):
    id: str                              # namespaced: "O1.m1"
    statement: str
    remedy: str = ""                     # the corrective idea / contrast case


class CheckItem(BaseModel):
    """A validated interactive check. ``question`` reuses the quiz IR, so grading,
    shuffling and "the answer never reaches the browser" are shared with quizzes."""

    id: str
    widget: str                          # a registered widget kind (services/learn/widgets.py)
    question: Question
    targets: list[str] = Field(default_factory=list)    # expectation / misconception ids
    reveal: str = ""                     # predict-then-reveal: shown after answering
    # MCQ: option index (as str) → misconception id the distractor exposes
    distractor_misconceptions: dict[str, str] = Field(default_factory=dict)


class Objective(BaseModel):
    """One knowledge component. ``detailed`` is False until the planner has filled in
    its teaching material (lazy planning: only the first objective is detailed up front)."""

    id: str                              # "O1"
    title: str
    kc_ref: str | None = None            # curriculum standard code → long-term mastery key
    prerequisites: list[str] = Field(default_factory=list)
    expectations: list[Expectation] = Field(default_factory=list)
    misconceptions: list[Misconception] = Field(default_factory=list)
    hook: str = ""                       # a mini-puzzle that opens the objective
    worked_example: str = ""             # hidden "teacher solution"
    hint_ladder: list[str] = Field(default_factory=list)  # fallback hints, least → most revealing
    checks: list[CheckItem] = Field(default_factory=list)
    challenge: CheckItem | None = None   # ⭐ stretch question for fast learners
    evidence: list[PinnedChunk] = Field(default_factory=list)
    detailed: bool = False

    @model_validator(mode="after")
    def _guardrails(self) -> Objective:
        if not self.detailed:
            return self
        if not self.expectations:
            raise ValueError(f"objective {self.id}: needs at least one expectation")
        if not self.checks:
            raise ValueError(f"objective {self.id}: needs at least one check — "
                             "an objective is never taught without being checked")
        known = {e.id for e in self.expectations} | {m.id for m in self.misconceptions}
        for c in [*self.checks, *([self.challenge] if self.challenge else [])]:
            bad = [t for t in c.targets if t not in known]
            if bad:
                raise ValueError(f"check {c.id} targets unknown ids {bad}")
        return self

    def expectation(self, eid: str) -> Expectation | None:
        return next((e for e in self.expectations if e.id == eid), None)


class PlanScope(BaseModel):
    grade: int | None = None
    subject: str | None = None
    language: str = "en"
    filters: dict[str, Any] = Field(default_factory=dict)
    collections: list[str] | None = None
    allow_fallback: bool = True          # the caller's ScopePolicy may forbid broadening
    ungrounded: bool = False             # nothing in the corpus — shown to the student


class LearningPlan(BaseModel):
    goal: str
    topic: str
    scope: PlanScope = Field(default_factory=PlanScope)
    hook: str = ""
    objectives: list[Objective] = Field(min_length=1)
    version: int = 1

    @model_validator(mode="after")
    def _dag(self) -> LearningPlan:
        ids = [o.id for o in self.objectives]
        if len(set(ids)) != len(ids):
            raise ValueError("objective ids must be unique")
        seen: set[str] = set()
        for o in self.objectives:   # objectives are listed in teaching order
            missing = [p for p in o.prerequisites if p not in seen]
            if missing:
                raise ValueError(f"objective {o.id}: prerequisites {missing} must come earlier")
            seen.add(o.id)
        return self

    def objective(self, oid: str) -> Objective:
        for o in self.objectives:
            if o.id == oid:
                return o
        raise KeyError(oid)


# ── live state ────────────────────────────────────────────────────────────────
# skipped = moved past with evidence (a passed check); left = moved past WITHOUT mastering
# (the student chose to, or the turn cap fired) — revisited by spaced review, never counted
ObjectiveStatus = Literal["pending", "active", "mastered", "skipped", "left"]
SessionStatus = Literal["planning", "active", "paused", "completed", "ended"]
Pace = Literal["slow", "normal", "fast"]


class ObjectiveState(BaseModel):
    status: ObjectiveStatus = "pending"
    coverage: dict[str, float] = Field(default_factory=dict)   # expectation id → 0..1
    misconceptions_seen: list[str] = Field(default_factory=list)
    hint_level: int = 0
    attempts: int = 0                    # failed tries since the last success
    p_mastery: float = 0.2
    early_evidence: bool = False         # covered while another objective was active
    checks_passed: int = 0
    used_checks: list[str] = Field(default_factory=list)
    tangents: int = 0
    explained: bool = False              # an explain_chunk / worked step was given
    turns: int = 0                       # tutor turns spent on this objective (loop breaker)


class PendingWidget(BaseModel):
    """The interactive check currently on screen, awaiting the student's answer."""

    check_id: str
    objective_id: str
    purpose: Literal["check", "challenge", "warm_up", "skip"] = "check"
    # Each DISPLAY of a check gets its own id: the client keys answers and grading by it,
    # so a check shown again can never inherit (or replay) an earlier answer.
    instance_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])


class LearningSession(BaseModel):
    id: str = Field(default_factory=_new_id)
    conversation_id: str
    owner_id: str
    status: SessionStatus = "planning"
    plan: LearningPlan
    cursor: str                          # the active objective id
    states: dict[str, ObjectiveState] = Field(default_factory=dict)
    pace: Pace = "normal"
    pending: PendingWidget | None = None
    parking_lot: list[str] = Field(default_factory=list)
    # rhythm signals that keep the dialogue lively (docs §5.6)
    questions_in_a_row: int = 0
    correct_streak: int = 0
    low_effort_streak: int = 0
    last_widget_kind: str | None = None
    turns_since_widget: int = 0
    recent_openings: list[str] = Field(default_factory=list)
    awaiting_choice: list[str] = Field(default_factory=list)   # objective ids offered
    choice_offered: bool = False         # a branch choice is offered at most once (W9)
    turn_count: int = 0
    version: int = 0                     # optimistic concurrency
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)

    @model_validator(mode="after")
    def _states(self) -> LearningSession:
        for o in self.plan.objectives:
            self.states.setdefault(o.id, ObjectiveState())
        if self.cursor not in self.states:
            raise ValueError(f"cursor {self.cursor!r} is not an objective")
        return self

    @property
    def objective(self) -> Objective:
        return self.plan.objective(self.cursor)

    @property
    def state(self) -> ObjectiveState:
        return self.states[self.cursor]

    def find_check(self, check_id: str) -> tuple[Objective, CheckItem] | None:
        for o in self.plan.objectives:
            for c in [*o.checks, *([o.challenge] if o.challenge else [])]:
                if c.id == check_id:
                    return o, c
        return None

    def mastered_count(self) -> int:
        return sum(1 for s in self.states.values() if s.status in ("mastered", "skipped"))

    def unused_checks(self, oid: str) -> list[CheckItem]:
        st = self.states[oid]
        return [c for c in self.plan.objective(oid).checks if c.id not in st.used_checks]

    def public(self) -> dict[str, Any]:
        """The student-safe view: titles, progress and phase — never the hidden
        teaching material (worked examples, answers, hints)."""
        return {
            "id": self.id, "conversation_id": self.conversation_id, "status": self.status,
            "goal": self.plan.goal, "topic": self.plan.topic,
            "ungrounded": self.plan.scope.ungrounded, "pace": self.pace, "cursor": self.cursor,
            "objectives": [
                {"id": o.id, "title": o.title, "status": self.states[o.id].status,
                 "early": self.states[o.id].early_evidence,
                 "mastery": round(self.states[o.id].p_mastery, 2)}
                for o in self.plan.objectives
            ],
            "mastered": self.mastered_count(), "total": len(self.plan.objectives),
            "parking_lot": list(self.parking_lot),
            "choices": list(self.awaiting_choice),
        }


def kc_key(objective: Objective, topic: str) -> str:
    """The long-term mastery key: the curriculum code when known, else a stable
    slug of topic + objective title."""
    if objective.kc_ref:
        return objective.kc_ref
    slug = lambda t: "-".join(t.casefold().split())[:60]
    return f"free:{slug(topic)}/{slug(objective.title)}"


# ── per-turn vocabulary ───────────────────────────────────────────────────────
class Act(str, Enum):
    answer = "answer"
    question_in_scope = "question_in_scope"
    question_off_topic = "question_off_topic"
    help_request = "help_request"        # "I don't get it", "just tell me"
    skip_request = "skip_request"        # "I already know this"
    stop_request = "stop_request"
    switch_topic = "switch_topic"
    chitchat = "chitchat"


class Correctness(str, Enum):
    correct = "correct"
    partial = "partial"
    incorrect = "incorrect"
    na = "n/a"


class Affect(str, Enum):
    neutral = "neutral"
    confused = "confused"
    frustrated = "frustrated"
    bored = "bored"
    confident = "confident"


class Trace(BaseModel):
    """What the student-state tracer read in a free-text turn."""

    act: Act = Act.answer
    correctness: Correctness = Correctness.na
    covered: dict[str, float] = Field(default_factory=dict)   # any objective's expectation ids
    misconception: str | None = None
    affect: Affect = Affect.neutral
    wants_answer: bool = False
    low_effort: bool = False
    move_on: bool = False                # "move on" / "next" — the student's call, honoured
    topic: str | None = None             # for switch_topic
    source: str = "rules"                # rules | llm


class QuickAction(str, Enum):
    """The chips under every tutor turn (docs §5.6 W4) — deterministic, no tracer."""

    hint = "hint"
    example = "example"
    skip = "skip"
    faster = "faster"
    slower = "slower"
    pause = "pause"
    end = "end"
    choose = "choose"                    # pick the next objective (value = objective id)
    resume = "resume"


class WidgetResult(BaseModel):
    check_id: str
    objective_id: str
    purpose: str
    correct: bool | None
    score: float
    expected: str = ""
    explanation: str = ""
    feedback: str = ""
    reveal: str = ""
    misconception: str | None = None
    confidence: int | None = None


class Observation(BaseModel):
    """One student turn, normalised: free text (traced), a widget answer (graded), or
    a quick action. Exactly one of ``trace`` / ``widget`` / ``action`` is set."""

    text: str = ""
    trace: Trace | None = None
    widget: WidgetResult | None = None
    action: QuickAction | None = None
    action_value: str | None = None
    early: list[str] = Field(default_factory=list)   # objectives newly credited early (learner sets)


class MoveKind(str, Enum):
    hook = "hook"
    explain_chunk = "explain_chunk"
    elicit = "elicit"
    pump = "pump"
    hint = "hint"
    worked_step = "worked_step"
    check = "check"
    confront = "confront"
    affirm_and_extend = "affirm_and_extend"
    reexplain = "reexplain"
    bottom_out = "bottom_out"
    challenge = "challenge"
    choose_next = "choose_next"
    recap_advance = "recap_advance"
    answer_tangent = "answer_tangent"
    park = "park"
    skip_check = "skip_check"
    pace_change = "pace_change"
    warm_up = "warm_up"
    pause = "pause"
    wrap_up = "wrap_up"


# moves allowed to state the answer outright (the leak guard skips them)
REVEALING_MOVES = frozenset({MoveKind.bottom_out, MoveKind.recap_advance, MoveKind.wrap_up,
                             MoveKind.affirm_and_extend, MoveKind.worked_step})


class Decision(BaseModel):
    """The policy's choice for this turn — WHAT to do; the renderer decides the words."""

    move: MoveKind
    objective_id: str
    hint_level: int = 0
    check_id: str | None = None          # a widget to show after the reply
    check_purpose: Literal["check", "challenge", "warm_up", "skip"] = "check"
    retrieve: Literal["none", "coverage_gap", "remediation"] = "none"
    notes: list[str] = Field(default_factory=list)   # e.g. "noticed_ahead:O3"
    advance_to: str | None = None        # move the cursor after this turn
    end_status: SessionStatus | None = None
    feedback: WidgetResult | None = None
    pace: Pace | None = None             # a pace change to apply
    check_next: bool = False             # after advancing, open the new objective with a check
    choices: list[str] = Field(default_factory=list)   # objectives offered (choose_next)
    switch_topic: str | None = None
    fresh_check: bool = False            # every check is used: ask the planner for a new one
