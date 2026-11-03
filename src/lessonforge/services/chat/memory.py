"""ConversationMemory — long-context handling for the chatbot.

Two complementary mechanisms, both config-driven (:class:`ChatMemoryConfig`):

- a **verbatim window** of the most recent turns (fed to condensation and shown
  to the synthesizer), so the immediate thread of conversation is exact; and
- a **rolling summary** of everything older than the window, folded in
  incrementally so the assistant keeps distant context without ever sending the
  full transcript to the model.

A "turn" is a user+assistant pair (2 messages); the config counts turns, this
module works in messages (``turns × 2``). The summary invariant is: *the summary
covers exactly the messages that have fallen out of the window*. It is folded
incrementally — each call summarizes only the span that newly left the window,
combined with the prior summary — so cost per turn stays bounded even in a long
chat. Summarization uses the fast model and degrades to a no-op on any error;
memory is an enhancement, never a hard dependency of answering.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ...config import ChatMemoryConfig
from ...domain.chat import ChatMessage, Conversation, Role
from ...providers.base import LLMClient
from .store import ChatStore


@dataclass(slots=True)
class MemoryView:
    recent: list[ChatMessage]  # the verbatim window, oldest-first
    summary: str               # rolling summary of everything older
    profile: list[str] = field(default_factory=list)    # L3: who the learner is (always on)
    relevant: list[str] = field(default_factory=list)   # L3: notes relevant to this question


_SUMMARY_SYSTEM = (
    "You maintain a running summary of a tutoring conversation. Given the summary "
    "so far and the new messages that scrolled out of view, produce an updated, "
    "concise summary that preserves facts, the user's goals, and any established "
    "preferences. Output only the summary."
)
# Structured variant: fixed sections the answer prompt can rely on, and that stop a
# long chat's summary from drifting into an unbounded story.
_STRUCTURED_SYSTEM = (
    "You maintain a running summary of a tutoring conversation. Given the summary so far "
    "and the new messages that scrolled out of view, output an updated summary in EXACTLY "
    "these sections (omit a section only if empty; at most 4 short bullets each):\n"
    "Goals: what the user is trying to do\n"
    "Facts established: answers and conclusions already reached\n"
    "Open questions: things asked but not yet resolved\n"
    "Preferences: how the user likes answers (language, length, examples)\n"
    "Output only the summary."
)


class ConversationMemory:
    def __init__(
        self, store: ChatStore, config: ChatMemoryConfig, *, llm: LLMClient
    ) -> None:
        self.store = store
        self.config = config
        self.llm = llm

    @property
    def window(self) -> int:
        """Verbatim window size, in messages."""
        return max(1, self.config.window_turns) * 2

    @property
    def trigger(self) -> int:
        """Summarize once the transcript exceeds this many messages."""
        return max(1, self.config.summary_trigger_turns) * 2

    def load(self, conversation: Conversation) -> MemoryView:
        """The window of recent messages + the stored rolling summary. Called
        before the current user turn is persisted, so it returns prior context."""
        recent, _ = self.store.tail(conversation.id, limit=self.window)
        return MemoryView(recent=recent, summary=conversation.summary)

    def maybe_summarize(
        self, conversation: Conversation, *, prev_len: int, total: int
    ) -> str | None:
        """Fold the messages that just left the window into the rolling summary.

        ``prev_len`` / ``total`` are the transcript length before and after this
        turn. They pin exactly which messages newly scrolled out
        (``[prev_boundary : new_boundary]``); only those are read and folded, so a
        long conversation never re-reads or re-summarises its whole history.
        Returns the new summary (already persisted) or ``None`` if nothing changed."""
        if not self.config.summarize or total <= self.trigger:
            return None
        prev_boundary = max(0, prev_len - self.window) if prev_len > self.trigger else 0
        new_boundary = total - self.window
        if new_boundary <= prev_boundary:
            return None
        # read the summary fresh: this runs as a background job, and the caller's
        # Conversation object was loaded before the previous fold may have finished
        fresh = self.store.get(conversation.id, owner_id=conversation.owner_id) or conversation
        departed = self.store.messages_between(conversation.id, prev_boundary, new_boundary)
        if not departed:
            return None
        transcript = "\n".join(
            f"{m.role.value.capitalize()}: {m.content}"
            for m in departed
            if m.role in (Role.user, Role.assistant)
        )
        prompt = (
            f"Summary so far:\n{fresh.summary or '(none)'}\n\n"
            f"New messages that scrolled out of view:\n{transcript}\n\nUpdated summary:"
        )
        try:
            system = _STRUCTURED_SYSTEM if self.config.structured_summary else _SUMMARY_SYSTEM
            summary = self.llm.complete(prompt, system=system, temperature=0.0).text.strip()
        except Exception:
            return None
        if not summary:
            return None
        self.store.update(conversation.id, owner_id=conversation.owner_id, summary=summary)
        return summary
