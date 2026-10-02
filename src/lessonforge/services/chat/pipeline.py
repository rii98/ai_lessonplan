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

from collections.abc import Iterator

from ...config import ChatConfig
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

    def stream(
        self, conversation: Conversation, request: ChatMessageRequest
    ) -> Iterator[ChatEvent]:
        # 1. memory: fetch prior transcript once (window view + length for summary).
        history = self.store.messages(conversation.id)
        prev_len = len(history)
        view = MemoryView(recent=history[-self.memory.window:], summary=conversation.summary)

        # 2. resolve the two-modes scope (per-message override wins over defaults).
        mode = request.mode or conversation.defaults.mode
        if request.filters is not None:
            filters = request.filters
        else:
            filters = conversation.defaults.filters(self.config.retrieval.filter_fields)
        collections = conversation.defaults.collections

        # 2b. intent: is this a plain question, or a request for a skill (a quiz…)?
        routed = self._route(request.message, history, conversation)
        if routed.skill != QA and routed.skill in self.skills:
            yield from self._run_skill(conversation, request, routed, history, view,
                                       prev_len, mode, filters)
            return

        # 3. transform → 4. retrieve+fuse → 5. build numbered context.
        transformed = self.transformer.transform(request.message, view.recent)
        chunks = self.retriever.retrieve(
            transformed, mode=mode, filters=filters, collections=collections
        )
        built = self.context_builder.build(transformed.standalone, chunks)

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
        all_messages = self.store.messages(conversation.id)
        self.memory.maybe_summarize(conversation, all_messages, prev_len=prev_len)

        yield ChatEvent("done", {
            "citations": len(built.citations),
            "mode": mode.value,
            "title": title,
            **transformed.meta(),
        })

    # ── intent routing + skills ──────────────────────────────────────────────
    def _route(
        self, message: str, history: list[ChatMessage], conversation: Conversation
    ) -> RoutedIntent:
        """Ask the router; any failure is a plain answer (routing must never break chat)."""
        if self.router is None or not self.skills:
            return RoutedIntent()
        try:
            last = history[-1] if history and history[-1].role is Role.assistant else None
            last_intent = (last.meta.get("intent") or {}) if last else {}
            state = RouterState(
                awaiting=last.meta.get("awaiting") if last else None,
                last_skill=last_intent.get("skill") if last_intent.get("skill") != QA else None,
                artifacts_note=self._artifacts_note(conversation),
                history=history[-6:],
            )
            return self.router.route(message, state)
        except Exception:
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
        self.memory.maybe_summarize(conversation, self.store.messages(conversation.id),
                                    prev_len=prev_len)
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
        self.store.update(conversation.id, title=title)
        return title
