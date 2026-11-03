"""LearnService — one learn-mode turn, end to end. Composition, not logic.

    observe (trace text │ grade widget │ quick action)
      → learner model (code)  → policy (code)  → apply decision (cursor, pace, hints…)
      → retrieval policy (usually none)  → render (LLM) + leak guard  → save + log
      → events: token · learn_widget · learn_state · learn_feedback · meta

The service depends only on ports (planner, tracer, learner, policy, retrieval,
renderer, guard, store, widgets) — each built from config and faked in tests. It
never writes chat messages: the :class:`ChatPipeline` persists the turn (exactly like
a skill turn), and captures the ``meta`` event onto the assistant message.

Long-term memory plugs in through three optional callables, so learn mode works with
memory switched off: ``prior`` (seed mastery), ``notes`` (personalise the voice) and
``on_end`` (commit mastery + an episode when a session finishes).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

from ...config import LearnConfig
from ...domain.chat import ChatMessage, Conversation, LearnInputRequest, WidgetResponse
from ...domain.learning import (
    Decision,
    LearningSession,
    MoveKind,
    Observation,
    PendingWidget,
    PlanScope,
    QuickAction,
    kc_key,
)
from ..chat.events import ChatEvent
from .guard import AnswerLeakGuard
from .learner import LearnerModel
from .planner import LessonPlanner, PlanningError
from .policy import ASKING, PedagogicalPolicy, enough_evidence, is_mastered, pick_check
from .renderer import RenderContext, TemplateRenderer, TutorRenderer, safe_line
from .retrieval import RetrievalPolicy
from .store import LearningSessionStore, SessionConflict
from .tracer import StudentStateTracer
from .widgets import WidgetService

log = logging.getLogger("lessonforge.learn")

ACTION_LABELS = {
    QuickAction.hint: "💡 Hint please", QuickAction.example: "📖 Show me an example",
    QuickAction.skip: "✅ I know this", QuickAction.faster: "⏩ Faster",
    QuickAction.slower: "🐢 Slower", QuickAction.pause: "⏸ Take a break",
    QuickAction.end: "⏹ End the lesson", QuickAction.choose: "👉 Let's do that",
    QuickAction.resume: "▶️ Continue the lesson",
}


@dataclass(slots=True)
class LearnTurn:
    """One student turn in a session: typed text, a widget answer, or a quick action."""

    text: str | None = None
    widget: WidgetResponse | None = None
    action: str | None = None
    action_value: str | None = None

    @classmethod
    def from_request(cls, req: LearnInputRequest) -> LearnTurn:
        return cls(widget=req.widget_response, action=req.action, action_value=req.action_value)


class LearnInputError(ValueError):
    """A learn-mode input that can't be accepted (expired widget, unknown action…)."""


class LearnService:
    def __init__(
        self, *, store: LearningSessionStore, planner: LessonPlanner, tracer: StudentStateTracer,
        learner: LearnerModel, policy: PedagogicalPolicy, retrieval: RetrievalPolicy,
        renderer: TutorRenderer, guard: AnswerLeakGuard, widgets: WidgetService,
        config: LearnConfig,
        prior: Callable[[str, list[str]], dict[str, float]] | None = None,
        notes: Callable[[str], list[str]] | None = None,
        on_end: Callable[[LearningSession], None] | None = None,
    ) -> None:
        self.store, self.planner, self.tracer = store, planner, tracer
        self.learner, self.policy, self.retrieval = learner, policy, retrieval
        self.renderer, self.guard, self.widgets = renderer, guard, widgets
        self.config = config
        self.prior, self.notes, self.on_end = prior, notes, on_end
        self._template = TemplateRenderer()

    # ── reading ──────────────────────────────────────────────────────────────
    def current(self, conversation_id: str) -> LearningSession | None:
        try:
            return self.store.current(conversation_id)
        except Exception:   # learn mode must never break ordinary chat
            log.exception("learning session lookup failed")
            return None

    @staticmethod
    def handles(session: LearningSession | None, turn: LearnTurn) -> bool:
        """Does this turn belong to the learning session? Active → every turn; paused →
        only an explicit resume (a paused lesson leaves the chat free for questions)."""
        if session is None:
            return False
        if session.status in ("active", "planning"):
            return True
        return session.status == "paused" and turn.action == QuickAction.resume.value

    def describe(self, session: LearningSession, turn: LearnTurn) -> str:
        """The student's turn as it should read in the transcript."""
        if turn.text is not None:
            return turn.text
        if turn.action is not None:
            try:
                act = QuickAction(turn.action)
            except ValueError:
                return turn.action
            if act is QuickAction.choose and turn.action_value:
                try:
                    return f"👉 {session.plan.objective(turn.action_value).title}"
                except KeyError:
                    pass
            return ACTION_LABELS.get(act, act.value)
        w = turn.widget
        assert w is not None
        pending = session.pending
        found = session.find_check(pending.check_id) \
            if pending is not None and pending.instance_id == w.widget_id else None
        if found is None:
            return "(answered a question)"
        q = found[1].question
        r = w.response
        if q.options and q.type.value == "mcq":
            try:
                return f"Answered: {q.options[int(r)]}"
            except (TypeError, ValueError, IndexError):
                pass
        if isinstance(r, list):
            return "Answered: (arranged the items)"
        return f"Answered: {str(r)[:200]}"

    # ── starting ─────────────────────────────────────────────────────────────
    def begin(self, conversation: Conversation, topic: str, scope: PlanScope, *,
              history: list[ChatMessage] | None = None) -> Iterator[ChatEvent]:
        """Plan a session (T0 retrieval + outline + first objective's detail) and open it."""
        current = self.current(conversation.id)
        if current is not None and current.status in ("planning", "active"):
            current.status = "paused"     # never two active sessions: the old one is resumable
            self._save(current)
        yield ChatEvent("status", "Planning your lesson…")
        owner = conversation.owner_id
        learner_notes = self._notes(owner)
        try:
            plan = self.planner.outline(topic, scope, learner_notes=learner_notes)
            yield ChatEvent("status", "Preparing the first part…")
            plan.objectives[0] = self.planner.detail(plan, plan.objectives[0].id)
        except PlanningError:
            log.exception("learn planning failed")
            yield ChatEvent("token", "Sorry — I couldn't prepare a lesson on that just now. "
                                     "Could you try again, or ask me about it directly?")
            return
        session = LearningSession(conversation_id=conversation.id, owner_id=owner,
                                  status="active", plan=plan, cursor=plan.objectives[0].id)
        session.states[session.cursor].status = "active"
        if self.prior is not None:
            try:
                self.learner.seed(session, self.prior(owner, [kc_key(o, plan.topic)
                                                              for o in plan.objectives]) or {})
            except Exception:
                log.exception("mastery prior failed")
        try:
            self.store.create(session)
        except SessionConflict:
            yield ChatEvent("error", {"code": "conflict", "message": "a lesson is already running here"})
            return
        decision = self.policy.opening(session)
        yield from self._execute(session, decision, Observation(), history or [],
                                 learner_notes=learner_notes, new=True)

    # ── one turn ─────────────────────────────────────────────────────────────
    def turn(self, conversation: Conversation, session: LearningSession,
             turn: LearnTurn, history: list[ChatMessage]) -> Iterator[ChatEvent]:
        try:
            obs = self._observe(session, turn, history)
        except LearnInputError as exc:
            yield ChatEvent("token", str(exc))
            yield ChatEvent("learn_state", session.public())
            return
        if session.status == "paused":
            session.status = "active"
        if obs.widget is not None:
            yield ChatEvent("learn_feedback", {
                "widget_id": turn.widget.widget_id if turn.widget else obs.widget.check_id,
                "correct": obs.widget.correct,
                "score": obs.widget.score, "expected": obs.widget.expected,
                "explanation": obs.widget.explanation, "feedback": obs.widget.feedback,
                "reveal": obs.widget.reveal})
        self.learner.apply(session, obs)
        decision = self.policy.decide(session, obs)
        yield from self._execute(session, decision, obs, history,
                                 learner_notes=self._notes(session.owner_id))
        if decision.switch_topic:
            scope = session.plan.scope.model_copy(update={"ungrounded": False})
            yield ChatEvent("token", "\n\n")
            yield from self.begin(conversation, decision.switch_topic, scope, history=history)

    def end(self, session: LearningSession) -> LearningSession:
        """End a session from the API (the ⏹ button outside a turn)."""
        session.status = "ended"
        self._save(session)
        self._finished(session)
        return session

    # ── internals ────────────────────────────────────────────────────────────
    def _observe(self, session: LearningSession, turn: LearnTurn,
                 history: list[ChatMessage]) -> Observation:
        if turn.action is not None:
            try:
                action = QuickAction(turn.action)
            except ValueError:
                raise LearnInputError("I didn't understand that button — try again?") from None
            return Observation(action=action, action_value=turn.action_value)
        if turn.widget is not None:
            w = turn.widget
            if session.pending is None or session.pending.instance_id != w.widget_id:
                raise LearnInputError("That question has already been answered — let's keep going!")
            result = self.widgets.grade(session, session.pending.check_id, w.response, w.confidence)
            if result is None:
                raise LearnInputError("I couldn't find that question — let's keep going!")
            session.pending = None
            return Observation(widget=result)
        text = turn.text or ""
        return Observation(text=text, trace=self.tracer.trace(session, text, history))

    def _execute(self, session: LearningSession, d: Decision, obs: Observation,
                 history: list[ChatMessage], *, learner_notes: list[str],
                 new: bool = False) -> Iterator[ChatEvent]:
        cfg = self.config.policy
        if d.fresh_check:   # every check used and none passed yet: make a new one, never re-show
            d = self._with_fresh_check(session, d)
        st = session.states[d.objective_id]
        st.turns += 1
        # 1. apply the decision to the structured state
        if d.pace:
            session.pace = d.pace
        if d.move is MoveKind.hint:
            st.hint_level = d.hint_level
        elif d.move is MoveKind.reexplain:
            st.hint_level = max(st.hint_level, d.hint_level)
        if d.move in (MoveKind.explain_chunk, MoveKind.worked_step, MoveKind.bottom_out,
                      MoveKind.reexplain):
            st.explained = True
        if d.move is MoveKind.answer_tangent:
            st.tangents += 1
        if d.move is MoveKind.park and obs.text:
            session.parking_lot = [*session.parking_lot, obs.text.strip()[:120]][-5:]
        if d.choices:
            session.awaiting_choice = list(d.choices)
            session.choice_offered = True
        if d.advance_to or d.end_status == "completed":
            prev = session.states[session.cursor]
            if is_mastered(session, session.cursor, cfg.mastered):
                prev.status = "mastered"
            elif enough_evidence(session, session.cursor, cfg.mastered) or prev.checks_passed >= 1:
                prev.status = "skipped"          # moved past with evidence
            else:
                prev.status = "left"             # moved past without it — spaced review brings it back
        if d.advance_to:
            yield ChatEvent("status", "Preparing the next part…")
            self._ensure_detail(session, d.advance_to)
            session.cursor = d.advance_to
            session.states[d.advance_to].status = "active"
            session.awaiting_choice = []
            if d.check_next:
                check = pick_check(session, d.advance_to)
                if check is not None:
                    update: dict[str, Any] = {"check_id": check.id, "check_purpose": "skip"}
                    if d.move is MoveKind.hook:      # a chosen objective they already showed
                        update["move"] = MoveKind.skip_check
                    d = d.model_copy(update=update)
        if d.end_status:
            session.status = d.end_status

        # 2. evidence for this turn (usually none — docs §4)
        try:
            ephemeral = self.retrieval.for_turn(session, d, obs.text)
        except Exception:
            ephemeral = []

        # 3. words (+ leak guard, + fallbacks so a session never stalls)
        text = self._render(RenderContext(session=session, decision=d, student_text=obs.text,
                                          history=history, ephemeral=ephemeral,
                                          learner_notes=learner_notes, max_hints=cfg.max_hints,
                                          action=ACTION_LABELS.get(obs.action) if obs.action else None))

        # 4. rhythm bookkeeping (docs §5.6)
        session.questions_in_a_row = (session.questions_in_a_row + 1
                                      if d.move in ASKING and not d.check_id else 0)
        session.turns_since_widget = 0 if d.check_id else session.turns_since_widget + 1
        opening = " ".join(text.split()[:5])
        session.recent_openings = [*session.recent_openings, opening][-3:]
        widget = None
        if d.check_id:
            found = session.find_check(d.check_id)
            if found is not None:
                obj, check = found
                session.pending = PendingWidget(check_id=check.id, objective_id=obj.id,
                                                purpose=d.check_purpose)
                session.last_widget_kind = check.widget
                if check.id not in session.states[obj.id].used_checks:
                    session.states[obj.id].used_checks.append(check.id)
                widget = self.widgets.play(check, purpose=d.check_purpose, objective_id=obj.id,
                                           confidence=d.check_purpose == "check",
                                           instance_id=session.pending.instance_id)

        # 5. persist, then show (a conflicting write must not show a reply)
        try:
            self._save(session)
        except SessionConflict:
            yield ChatEvent("error", {"code": "conflict",
                                      "message": "this lesson changed in another tab — refresh to continue"})
            return
        self.store.log(session.id, {
            "turn": session.turn_count, "move": d.move.value, "objective": d.objective_id,
            "notes": d.notes, "check": d.check_id, "retrieval": getattr(self.retrieval, "last_trigger", "none"),
            "trace": obs.trace.model_dump(mode="json") if obs.trace else None,
            "widget": obs.widget.model_dump(mode="json") if obs.widget else None,
            "action": obs.action.value if obs.action else None,
        })
        yield ChatEvent("token", text)
        if widget is not None:
            yield ChatEvent("learn_widget", widget)
        yield ChatEvent("learn_state", session.public())
        yield ChatEvent("meta", {"learn": {"session_id": session.id, "move": d.move.value,
                                           "objective": d.objective_id, "widget_id": d.check_id,
                                           "new_session": new}})
        if session.status in ("completed", "ended"):
            self._finished(session)

    def _render(self, ctx: RenderContext) -> str:
        """Model → targeted re-ask → template → a safe line. Every candidate passes the
        leak guard; a re-ask names the phrase to avoid (rather than a vague "don't
        reveal it"), which is what makes a second draft actually differ."""
        for attempt in range(2):
            ctx.strict = attempt > 0
            try:
                text = self.renderer.render(ctx)
            except Exception:
                log.warning("tutor renderer failed; using the template", exc_info=True)
                break
            leaked = self.guard.find(text, ctx.session, ctx.decision)
            if leaked is None:
                return text
            ctx.avoid = [*ctx.avoid, leaked]
            log.info("leak guard fired (move %s)", ctx.decision.move.value)
        for text in (self._template.render(ctx), safe_line(ctx)):
            if not self.guard.leaked(text, ctx.session, ctx.decision):
                return text
        return "Let's try this one:" if ctx.decision.check_id else "Let's keep going — what do you think?"

    def _with_fresh_check(self, session: LearningSession, d: Decision) -> Decision:
        obj = session.plan.objective(d.objective_id)
        try:
            check = self.planner.fresh_check(session.plan, obj.id)
        except Exception:
            log.exception("fresh check failed")
            check = None
        if check is None or check.id in session.states[obj.id].used_checks:
            # nothing new to ask: give the key idea and move on (revisited by spaced review)
            return self.policy.advance(session, notes=("moving_on",))
        obj.checks.append(check)
        return d.model_copy(update={"check_id": check.id, "fresh_check": False})

    def _ensure_detail(self, session: LearningSession, oid: str) -> None:
        obj = session.plan.objective(oid)
        if obj.detailed:
            return
        detailed = self.planner.detail(session.plan, oid)
        session.plan.objectives = [detailed if o.id == oid else o for o in session.plan.objectives]

    def _save(self, session: LearningSession) -> None:
        self.store.save(session)

    def _notes(self, owner_id: str) -> list[str]:
        if self.notes is None:
            return []
        try:
            return list(self.notes(owner_id))[:5]
        except Exception:
            return []

    def _finished(self, session: LearningSession) -> None:
        if self.on_end is None:
            return
        try:
            self.on_end(session)
        except Exception:
            log.exception("learn on_end hook failed")


def scope_from(filters: dict[str, Any], collections: list[str] | None, *, allow_fallback: bool,
               language: str = "en") -> PlanScope:
    grade = filters.get("grade")
    try:
        grade = int(grade) if grade is not None else None
    except (TypeError, ValueError):
        grade = None
    return PlanScope(grade=grade if grade and 1 <= grade <= 12 else None,
                     subject=filters.get("subject"), language=language,
                     filters=dict(filters), collections=collections,
                     allow_fallback=allow_fallback)
