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

import time
from collections.abc import Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

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
    Role,
)
from .artifacts import ArtifactService
from .context import ContextBuilder
from .events import ChatEvent
from .intent import IntentRouter, RouterState
from .memory import ConversationMemory, MemoryView
from .retrieve import ChatRetriever
from .skills.base import QA, RoutedIntent, Skill, SkillContext
from .synthesize import AnswerSynthesizer
from .transform import QueryTransformer

__all__ = ["ChatEvent", "ChatPipeline"]  # ChatEvent re-exported: the API imports it from here

_ROUTER_HISTORY = 6   # recent messages the intent router sees


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
        # The router runs concurrently with query-transform + retrieval (see stream),
        # so classifying EVERY turn costs ~no latency.
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="intent")

    def stream(
        self, conversation: Conversation, request: ChatMessageRequest, *,
        turn: TurnScope | None = None, allowed_skills: frozenset[str] | None = None,
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
                                       prev_len, mode, filters, started)
            return
        if prep_error is not None:
            raise prep_error

        # 6. persist the user turn (with the scope actually used, for auditing).
        user_meta = {"mode": mode.value, "filters": filters}
        self.store.add_message(
            conversation.id,
            ChatMessage(role=Role.user, content=request.message, meta=user_meta),
        )

        # 7. stream the grounded answer, accumulating it for persistence.
        answer_parts: list[str] = []
        try:
            for piece in self.synthesizer.stream(request.message, built, view):
                answer_parts.append(piece)
                yield ChatEvent("token", piece)
        except Exception as exc:  # streaming can't be retried mid-answer
            yield ChatEvent("error", {"message": str(exc)})
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

        # 10. title a fresh conversation from its first question; roll the summary.
        title = self._maybe_title(conversation, prev_len, request.message)
        self.memory.maybe_summarize(conversation, prev_len=prev_len, total=prev_len + 2)
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
        started: float,
    ) -> Iterator[ChatEvent]:
        """Run one skill turn: persist the user message, stream the skill's events
        (saving any artifact as it appears), then persist the assistant turn with the
        routing decision, the artifacts it made, and any slot it is waiting on."""
        self.store.add_message(conversation.id, ChatMessage(
            role=Role.user, content=request.message,
            meta={"mode": mode.value, "filters": filters}))
        yield ChatEvent("intent", routed.meta())

        reply = ChatMessage(role=Role.assistant, content="")
        parts: list[str] = []
        artifact_ids: list[str] = []
        awaiting = None
        skill = self.skills[routed.skill]
        ctx = SkillContext(conversation=conversation, message=request.message, routed=routed,
                           history=view.recent, summary=view.summary, deps=skill.deps)
        try:
            for ev in skill.run(ctx):
                if ev.type == "token":
                    parts.append(ev.data)
                    yield ev
                elif ev.type == "artifact":
                    art = self._save_artifact(ev.data, reply.id)
                    artifact_ids.append(art.id)
                    self._record_artifact(art)
                    yield ChatEvent("artifact", self._artifact_payload(art))
                elif ev.type == "awaiting":
                    awaiting = ev.data
                else:  # status, … — pass through
                    yield ev
        except Exception as exc:  # a skill bug must not lose the turn
            yield ChatEvent("error", {"message": str(exc)})

        reply.content = "".join(parts)
        reply.meta = {"mode": mode.value, "intent": routed.meta()}
        if artifact_ids:
            reply.meta["artifact_ids"] = artifact_ids
        if awaiting:
            reply.meta["awaiting"] = awaiting
        self.store.add_message(conversation.id, reply)

        title = self._maybe_title(conversation, prev_len, request.message)
        self.memory.maybe_summarize(conversation, prev_len=prev_len, total=prev_len + 2)
        self._record_turn(routed, filters, conversation, [], started)
        yield ChatEvent("done", {"citations": 0, "mode": mode.value, "title": title,
                                 "intent": routed.meta(), "artifacts": artifact_ids})

    def _save_artifact(self, artifact: ChatArtifact, message_id: str) -> ChatArtifact:
        artifact.message_id = message_id
        return self.store.add_artifact(artifact)

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
        title = first_message.strip().splitlines()[0][:60] or "New chat"
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


def _unique(values) -> list[str]:
    return list(dict.fromkeys(v for v in values if v))
