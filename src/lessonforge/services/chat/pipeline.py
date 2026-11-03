"""ChatPipeline — orchestrates one chat turn end to end.

Composition, not logic: it wires the single-responsibility stages together and
yields a stream of :class:`ChatEvent`s the API renders as SSE::

    load memory ─► transform query ─► retrieve+fuse ─► build context
                ─► stream synthesis ─► persist turn ─► roll summary

The pipeline depends only on the stage interfaces (a :class:`ChatStore`, a
:class:`QueryTransformer`, a :class:`ChatRetriever`, a :class:`ContextBuilder`, a
:class:`ConversationMemory`, an :class:`AnswerSynthesizer`) — so any of them can
be swapped or faked without touching this file.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from typing import TYPE_CHECKING, Any

from ... import request_context
from ...activity import events as act
from ...activity.store import ActivityRecorder
from ...config import ChatConfig
from ...context.scope_policy import TurnScope
from ...domain.chat import (
    ChatArtifact,
    ChatMessage,
    ChatMessageRequest,
    Conversation,
    LearnInputRequest,
    Role,
    StartLearningRequest,
)
from .artifacts import ArtifactService
from .context import ContextBuilder
from .events import ChatEvent
from .intent import IntentRouter, RouterState
from .memory import ConversationMemory, MemoryView
from .retrieve import ChatRetriever
from .skills.base import QA, RoutedIntent, Skill, SkillContext

if TYPE_CHECKING:  # learn mode / long-term memory are optional
    from ..learn import LearnService
    from ..memory import MemoryJobQueue, MemoryService
from .synthesize import AnswerSynthesizer
from .transform import QueryTransformer

__all__ = ["ChatEvent", "ChatPipeline"]  # ChatEvent re-exported: the API imports it from here

_ROUTER_HISTORY = 6   # recent messages the intent router sees


log = logging.getLogger("lessonforge.chat")

class ChatPipeline:
    def __init__(
        self,
        *,
        store,
        memory: ConversationMemory,
        transformer: QueryTransformer,
        retriever: ChatRetriever,
        context_builder: ContextBuilder,
        synthesizer: AnswerSynthesizer,
        config: ChatConfig,
        router: IntentRouter | None = None,
        skills: dict[str, Skill] | None = None,
        artifacts: ArtifactService | None = None,
        activity: ActivityRecorder | None = None,
        learn: LearnService | None = None,
        jobs: MemoryJobQueue | None = None,
        long_term: MemoryService | None = None,
    ) -> None:
        self.store = store
        self.memory = memory
        self.transformer = transformer
        self.retriever = retriever
        self.context_builder = context_builder
        self.synthesizer = synthesizer
        self.config = config
        # Skills (quiz, …) are optional: with no router the pipeline is exactly the
        # plain grounded-answer flow it always was.
        self.router = router
        self.skills = skills or {}
        self.artifacts = artifacts
        self.activity = activity
        # Learn mode (docs/LEARN_MODE.md): while a session is active it owns the turn.
        self.learn = learn
        # Memory upkeep (summary folds, L3 extraction) runs off the request path.
        if jobs is None:
            from ..memory import InlineJobs
            jobs = InlineJobs()
        self.jobs = jobs
        self.long_term = long_term
        # The router runs concurrently with query-transform + retrieval (see stream),
        # so classifying EVERY turn costs ~no latency.
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="intent")

    def stream(
        self, conversation: Conversation, request: ChatMessageRequest, *,
        turn: TurnScope | None = None, allowed_skills: frozenset[str] | None = None,
        language: str = "en",
    ) -> Iterator[ChatEvent]:
        """One turn. ``turn`` carries server-side scope constraints (ScopePolicy);
        ``allowed_skills`` the skills this caller may use (``None`` = all)."""
        started = time.perf_counter()
        turn = turn or TurnScope()
        # 1. memory: only the transcript TAIL a turn needs (window + router view) and
        #    the transcript length — never the whole history.
        history, prev_len = self.store.tail(
            conversation.id, limit=max(self.memory.window, _ROUTER_HISTORY))
        view = MemoryView(recent=history[-self.memory.window:], summary=conversation.summary)

        # 1b. an active learning session owns the turn (no router, no QA retrieval —
        #     the session's plan decides; docs/LEARN_MODE.md §2.1)
        if self.learn is not None:
            from ..learn import LearnTurn

            lt = LearnTurn(text=request.message)
            session = self.learn.current(conversation.id)
            if self.learn.handles(session, lt):
                yield from self._run_learn(conversation, lt, session, history, prev_len, started)
                return

        # 2. resolve the two-modes scope (per-message override wins over defaults),
        #    then let the caller's scope policy constrain it (``locked`` overrides).
        mode = request.mode or conversation.defaults.mode
        if request.filters is not None:
            filters = request.filters
        else:
            filters = conversation.defaults.filters(self.config.retrieval.filter_fields)
        mode, filters, collections = turn.apply(mode, dict(filters),
                                                conversation.defaults.collections)

        # 2b. intent: start routing NOW, in parallel with the (slower) QA preparation
        # below. A turn that turns out to be a skill request throws the QA work away;
        # a plain question has already done it, so routing adds ~no latency.
        route_future = self._submit_route(request.message, history, conversation, allowed_skills)

        # 3. transform → 4. retrieve+fuse → 5. build numbered context.
        prep_error: Exception | None = None
        transformed = built = None
        try:
            transformed = self.transformer.transform(request.message, view.recent)
            chunks = self.retriever.retrieve(
                transformed, mode=mode, filters=filters, collections=collections,
                allow_fallback=turn.allow_broad_fallback,
            )
            built = self.context_builder.build(transformed.standalone, chunks)
        except Exception as exc:  # only fatal if this turns out to be a plain question
            prep_error = exc

        routed = self._await_route(route_future)
        if routed.skill != QA and routed.skill in self.skills and (
                allowed_skills is None or routed.skill in allowed_skills):
            yield from self._run_skill(conversation, request, routed, history, view,
                                       prev_len, mode, filters, started,
                                       collections=collections, turn=turn, language=language)
            return
        if prep_error is not None:
            raise prep_error

        # 5b. long-term memory: a few notes about the learner (personalise only)
        if self.long_term is not None:
            view.profile = self.long_term.notes_for(conversation.owner_id, transformed.standalone)

        # 6. persist the user turn (with the scope actually used, for auditing).
        user_meta = {"mode": mode.value, "filters": filters}
        user_msg = self.store.add_message(
            conversation.id,
            ChatMessage(role=Role.user, content=request.message, meta=user_meta),
        )

        # 7. stream the grounded answer, accumulating it for persistence.
        answer_parts: list[str] = []
        try:
            for piece in self.synthesizer.stream(request.message, built, view):
                answer_parts.append(piece)
                yield ChatEvent("token", piece)
        except Exception:  # streaming can't be retried mid-answer; internals stay in the log
            log.exception("chat answer stream failed")
            yield ChatEvent("error", {"code": "internal", "message": "the answer was cut off"})
            # still persist what we have so the turn isn't lost
        answer = "".join(answer_parts)

        # 8. citations for the UI (each carries its chunk text for preview).
        yield ChatEvent("sources", [c.model_dump() for c in built.citations])

        # 9. persist the assistant turn with its citations + diagnostics.
        assistant_meta = {"mode": mode.value, **transformed.meta()}
        if self.router is not None:
            assistant_meta["intent"] = routed.meta()
        self.store.add_message(
            conversation.id,
            ChatMessage(
                role=Role.assistant, content=answer,
                citations=built.citations, meta=assistant_meta,
            ),
        )

        # 10. title a fresh conversation from its first question; roll the summary and
        #     learn from what the user said — both queued, off the request path.
        title = self._maybe_title(conversation, prev_len, request.message)
        self._after_turn(conversation, prev_len, request.message, user_msg.id, history)
        self._record_turn(routed, filters, conversation, built.citations, started)

        yield ChatEvent("done", {
            "citations": len(built.citations),
            "mode": mode.value,
            "title": title,
            **transformed.meta(),
        })

    # ── intent routing + skills ──────────────────────────────────────────────
    def _submit_route(
        self, message: str, history: list[ChatMessage], conversation: Conversation,
        allowed: frozenset[str] | None = None,
    ) -> Future[RoutedIntent] | None:
        """Build the router's view of the conversation (on this thread — the store is
        not touched from workers) and start classifying in the background. The
        worker inherits the request context (usage metering, scope)."""
        if self.router is None or not self.skills:
            return None
        try:
            last = history[-1] if history and history[-1].role is Role.assistant else None
            last_intent = (last.meta.get("intent") or {}) if last else {}
            state = RouterState(
                awaiting=last.meta.get("awaiting") if last else None,
                last_skill=last_intent.get("skill") if last_intent.get("skill") != QA else None,
                artifacts_note=self._artifacts_note(conversation),
                history=history[-_ROUTER_HISTORY:],
                allowed=allowed,
            )
            return request_context.submit_in_context(self._pool, self._safe_route, message, state)
        except Exception:
            return None

    def _safe_route(self, message: str, state: RouterState) -> RoutedIntent:
        try:
            return self.router.route(message, state)  # type: ignore[union-attr]
        except Exception:
            return RoutedIntent()

    def _await_route(self, future: Future[RoutedIntent] | None) -> RoutedIntent:
        """The decision, or a plain answer if routing is unavailable or too slow."""
        if future is None:
            return RoutedIntent()
        try:
            return future.result(timeout=self.config.intent.timeout_s)
        except Exception:  # timeout or worker failure: fail open
            future.cancel()
            return RoutedIntent()

    def _artifacts_note(self, conversation: Conversation) -> str:
        if self.artifacts is None:
            return ""
        try:
            made = self.artifacts.summaries(conversation.id)[-3:]
        except Exception:
            return ""
        if not made:
            return ""
        lines = [f"- {m['kind']} \"{m['title']}\" ({m['question_count']} questions"
                 + (f", best score {m['best']:g}" if m.get("best") is not None else "") + ")"
                 for m in made]
        return "Artifacts already made in this chat:\n" + "\n".join(lines)

    def _run_skill(
        self, conversation: Conversation, request: ChatMessageRequest, routed: RoutedIntent,
        history: list[ChatMessage], view: MemoryView, prev_len: int, mode, filters,
        started: float, *, collections: list[str] | None = None, turn: TurnScope | None = None,
        language: str = "en",
    ) -> Iterator[ChatEvent]:
        """Run one skill turn: persist the user message, stream the skill's events
        (saving any artifact as it appears), then persist the assistant turn with the
        routing decision, the artifacts it made, and any slot it is waiting on."""
        user_msg = self.store.add_message(conversation.id, ChatMessage(
            role=Role.user, content=request.message,
            meta={"mode": mode.value, "filters": filters}))
        yield ChatEvent("intent", routed.meta())
        skill = self.skills[routed.skill]
        ctx = SkillContext(conversation=conversation, message=request.message, routed=routed,
                           history=view.recent, summary=view.summary, deps=skill.deps,
                           filters=dict(filters), collections=collections,
                           allow_fallback=(turn or TurnScope()).allow_broad_fallback,
                           language=language)
        yield from self._relay(conversation, _deferred(skill.run, ctx), what=f"skill {routed.skill!r}",
                               meta={"mode": mode.value, "intent": routed.meta()},
                               prev_len=prev_len, title_text=request.message,
                               routed=routed, filters=filters, started=started,
                               said=(request.message, user_msg.id), prior=history)

    # ── learn mode ───────────────────────────────────────────────────────────
    def learn_input(self, conversation: Conversation, request: LearnInputRequest) -> Iterator[ChatEvent]:
        """A widget answer or quick action in the conversation's learning session."""
        if self.learn is None:
            yield ChatEvent("error", {"code": "disabled", "message": "learn mode is not enabled"})
            return
        from ..learn import LearnTurn

        started = time.perf_counter()
        lt = LearnTurn.from_request(request)
        session = self.learn.current(conversation.id)
        if not self.learn.handles(session, lt):
            yield ChatEvent("error", {"code": "no_session",
                                      "message": "there is no lesson running in this chat"})
            return
        history, prev_len = self.store.tail(conversation.id, limit=self.memory.window)
        yield from self._run_learn(conversation, lt, session, history, prev_len, started)

    def _run_learn(self, conversation: Conversation, lt, session,
                   history: list[ChatMessage], prev_len: int, started: float) -> Iterator[ChatEvent]:
        assert self.learn is not None
        user_meta: dict[str, Any] = {"mode": "learn", "learning_session": session.id}
        if lt.widget is not None:
            user_meta["widget_response"] = lt.widget.model_dump(mode="json")
        if lt.action is not None:
            user_meta["action"] = {"name": lt.action, "value": lt.action_value}
        user_msg = self.store.add_message(conversation.id, ChatMessage(
            role=Role.user, content=self.learn.describe(session, lt), meta=user_meta))
        routed = RoutedIntent(skill="learn", source="session")
        yield from self._relay(conversation, _deferred(self.learn.turn, conversation, session, lt, history),
                               what="learn turn", meta={"mode": "learn", "intent": routed.meta()},
                               prev_len=prev_len, title_text=session.plan.topic, routed=routed,
                               filters=session.plan.scope.filters, started=started,
                               said=(lt.text, user_msg.id) if lt.text else None, prior=history)

    def start_learning(self, conversation: Conversation, request: StartLearningRequest, *,
                       turn: TurnScope | None = None, language: str = "en") -> Iterator[ChatEvent]:
        """Open a learn-mode session from the composer's Learn toggle / "Teach me this"."""
        if self.learn is None:
            yield ChatEvent("error", {"code": "disabled", "message": "learn mode is not enabled"})
            return
        from ..learn import scope_from

        started = time.perf_counter()
        turn = turn or TurnScope()
        history, prev_len = self.store.tail(conversation.id, limit=self.memory.window)
        mode = request.mode or conversation.defaults.mode
        filters = (request.filters if request.filters is not None
                   else conversation.defaults.filters(self.config.retrieval.filter_fields))
        mode, filters, collections = turn.apply(mode, dict(filters), conversation.defaults.collections)
        if mode.value == "broad":
            filters = {}
        scope = scope_from(filters, collections, allow_fallback=turn.allow_broad_fallback,
                           language=language)
        self.store.add_message(conversation.id, ChatMessage(
            role=Role.user, content=f"Teach me: {request.topic}",
            meta={"mode": "learn", "filters": filters}))
        routed = RoutedIntent(skill="learn", source="explicit")
        yield ChatEvent("intent", routed.meta())
        yield from self._relay(conversation,
                               _deferred(self.learn.begin, conversation, request.topic, scope,
                                         history=history),
                               what="learn start", meta={"mode": "learn", "intent": routed.meta()},
                               prev_len=prev_len, title_text=request.topic, routed=routed,
                               filters=filters, started=started)

    # ── shared: relay a turn's events, then persist it ───────────────────────
    def _relay(self, conversation: Conversation, events: Iterator[ChatEvent], *, what: str,
               meta: dict[str, Any], prev_len: int, title_text: str, routed: RoutedIntent,
               filters: dict[str, Any], started: float,
               said: tuple[str, str] | None = None,
               prior: list[ChatMessage] | None = None) -> Iterator[ChatEvent]:
        """Stream a non-QA turn (a skill or learn mode): forward its events, saving any
        artifact as it appears; ``meta`` events are captured onto the assistant message
        (not forwarded). Then persist the reply, title the chat, roll the summary."""
        reply = ChatMessage(role=Role.assistant, content="")
        parts: list[str] = []
        artifact_ids: list[str] = []
        # Artifacts reference their assistant message (a foreign key in Postgres),
        # so they are streamed as they appear but persisted after the reply is.
        pending: list[ChatArtifact] = []
        awaiting = None
        extra: dict[str, Any] = {}
        try:
            for ev in events:
                if ev.type == "token":
                    parts.append(ev.data)
                    yield ev
                elif ev.type == "artifact":
                    art = ev.data
                    art.message_id = reply.id
                    pending.append(art)
                    artifact_ids.append(art.id)
                    yield ChatEvent("artifact", self._artifact_payload(art))
                elif ev.type == "awaiting":
                    awaiting = ev.data
                elif ev.type == "meta":
                    extra.update(ev.data or {})
                else:  # status, learn_*, … — pass through
                    yield ev
        except Exception:  # a bug must not lose the turn — nor leak its internals
            log.exception("chat %s failed", what)
            yield ChatEvent("error", {"code": "internal", "message": "the assistant could not finish this"})

        reply.content = "".join(parts)
        reply.meta = {**meta, **extra}
        if artifact_ids:
            reply.meta["artifact_ids"] = artifact_ids
        if awaiting:
            reply.meta["awaiting"] = awaiting
        self.store.add_message(conversation.id, reply)
        for art in pending:
            self._record_artifact(self.store.add_artifact(art))

        title = self._maybe_title(conversation, prev_len, title_text)
        self._after_turn(conversation, prev_len, *(said or (None, None)), prior)
        self._record_turn(routed, filters, conversation, [], started)
        done: dict[str, Any] = {"citations": 0, "mode": meta.get("mode"), "title": title,
                                "intent": routed.meta(), "artifacts": artifact_ids}
        if "learn" in extra:
            done["learn"] = extra["learn"]
        yield ChatEvent("done", done)

    def _after_turn(self, conversation: Conversation, prev_len: int, said: str | None,
                    message_id: str | None, prior: list[ChatMessage] | None = None) -> None:
        """Queue the memory upkeep for a finished turn (never on the request path)."""
        self.jobs.submit(self.memory.maybe_summarize, conversation,
                         prev_len=prev_len, total=prev_len + 2)
        if self.long_term is not None and said:
            self.long_term.remember_turn(conversation.owner_id, conversation.id, said, message_id,
                                         context=_prior_turns(prior or []))

    def _artifact_payload(self, artifact: ChatArtifact) -> dict:
        if self.artifacts is not None:
            return self.artifacts.summary(artifact)
        return {"id": artifact.id, "kind": artifact.kind, "title": artifact.title}

    def _maybe_title(
        self, conversation: Conversation, prev_len: int, first_message: str
    ) -> str | None:
        """Name a brand-new conversation after its first user message."""
        if prev_len > 0 or conversation.title not in ("", "New chat"):
            return None
        title = (first_message.strip().splitlines() or [""])[0][:60] or "New chat"
        self.store.update(conversation.id, owner_id=conversation.owner_id, title=title)
        return title

    # ── activity (content-free; see activity/events.py) ──────────────────────
    def _record_turn(self, routed: RoutedIntent, filters: dict[str, Any],
                     conversation: Conversation, citations: list[Any], started: float) -> None:
        if self.activity is None:
            return
        ctx = request_context.current()
        usage = ctx.usage.snapshot() if ctx else {}
        metas = [c.metadata or {} for c in citations]
        subject = filters.get("subject") or conversation.defaults.subject
        try:
            props = act.ChatMessage(
                intent=routed.skill, intent_source=routed.source,
                confidence=max(0.0, min(1.0, routed.confidence)),
                subject=str(subject)[:160] if subject else None,
                chapters=_unique(str(m["chapter"])[:160] for m in metas if m.get("chapter"))[:20],
                collections=_unique(c.collection for c in citations)[:10],
                spaces_hit=_unique(str(m["space"]) for m in metas if m.get("space"))[:10],
                citations=len(citations), grounded=bool(citations),
                latency_ms=int((time.perf_counter() - started) * 1000),
                tokens_in=usage.get("tokens_in", 0), tokens_out=usage.get("tokens_out", 0),
                model=usage.get("model"),
            )
        except Exception:   # an odd value must never fail the turn
            return
        self.activity.record(act.build_event("chat.message", props, resource_type="conversation",
                                            resource_id=conversation.id))

    def _record_artifact(self, art: ChatArtifact) -> None:
        if self.activity is None or art.kind != "quiz":
            return
        questions = art.payload.get("questions") or []
        spec = art.payload.get("spec") or {}
        try:
            props = act.QuizCreated(
                source="chat", questions=len(questions),
                question_types=_unique(str(q.get("type")) for q in questions
                                       if isinstance(q, dict) and q.get("type"))[:20],
                difficulty=str(spec.get("difficulty")) if spec.get("difficulty") else None,
                subject=(art.payload.get("curriculum_ref") or {}).get("subject"),
                chapter=art.meta.get("chapter"),
            )
        except Exception:
            return
        self.activity.record(act.build_event("quiz.created", props, resource_type="quiz",
                                            resource_id=art.id))


_MEMORY_CONTEXT = 2   # the previous exchange: enough to resolve "the above", "yes, that one"


def _prior_turns(history: list[ChatMessage]):
    """The messages just before the student's new one, for the memory extractor."""
    from ...domain.memory import PriorTurn

    turns = [m for m in history if m.role in (Role.user, Role.assistant) and m.content.strip()]
    return [PriorTurn(role=m.role.value, text=m.content) for m in turns[-_MEMORY_CONTEXT:]]


def _deferred(fn, *args, **kwargs) -> Iterator[ChatEvent]:
    """Call ``fn`` only when iterated, so a callee that raises before yielding is caught
    by the relay's error handling like any mid-stream failure."""
    yield from fn(*args, **kwargs)


def _unique(values) -> list[str]:
    return list(dict.fromkeys(v for v in values if v))
