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
from dataclasses import dataclass
from typing import Any

from ...config import ChatConfig
from ...domain.chat import (
    ChatMessage,
    ChatMessageRequest,
    Conversation,
    Role,
)
from .context import ContextBuilder
from .memory import ConversationMemory, MemoryView
from .retrieve import ChatRetriever
from .synthesize import AnswerSynthesizer
from .transform import QueryTransformer


@dataclass(slots=True)
class ChatEvent:
    """A streamed event. ``type`` is one of ``token`` | ``sources`` | ``done`` |
    ``error``; ``data`` is a token string, a citation list, or a meta dict."""

    type: str
    data: Any


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
    ) -> None:
        self.store = store
        self.memory = memory
        self.transformer = transformer
        self.retriever = retriever
        self.context_builder = context_builder
        self.synthesizer = synthesizer
        self.config = config

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

    def _maybe_title(
        self, conversation: Conversation, prev_len: int, first_message: str
    ) -> str | None:
        """Name a brand-new conversation after its first user message."""
        if prev_len > 0 or conversation.title not in ("", "New chat"):
            return None
        title = first_message.strip().splitlines()[0][:60] or "New chat"
        self.store.update(conversation.id, title=title)
        return title
