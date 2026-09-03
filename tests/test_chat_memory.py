"""ConversationMemory: verbatim window + incremental rolling summary."""

from __future__ import annotations

from lessonforge.config import ChatMemoryConfig
from lessonforge.domain.chat import ChatMessage, Conversation, Role
from lessonforge.providers.base import LLMClient, LLMResult
from lessonforge.services.chat.memory import ConversationMemory
from lessonforge.services.chat.store import MemoryChatStore


class CapturingLLM(LLMClient):
    def __init__(self, summary="ROLLED UP"):
        self.summary = summary
        self.prompts: list[str] = []

    def complete(self, prompt, *, system=None, json_schema=None, temperature=None) -> LLMResult:
        self.prompts.append(prompt)
        return LLMResult(text=self.summary)

    def health(self) -> bool:
        return True


def _mem(store, llm, **kw):
    cfg = ChatMemoryConfig(**{"window_turns": 1, "summarize": True, "summary_trigger_turns": 2, **kw})
    return ConversationMemory(store, cfg, llm=llm)


def _seed(store, n):
    conv = store.create(Conversation())
    for i in range(n):
        role = Role.user if i % 2 == 0 else Role.assistant
        store.add_message(conv.id, ChatMessage(role=role, content=f"m{i}"))
    return store.get(conv.id)


def test_load_returns_recent_window_and_summary():
    store = MemoryChatStore()
    conv = _seed(store, 6)
    store.update(conv.id, summary="earlier stuff")
    view = _mem(store, CapturingLLM()).load(store.get(conv.id))
    assert [m.content for m in view.recent] == ["m4", "m5"]  # window_turns=1 → 2 messages
    assert view.summary == "earlier stuff"


def test_no_summary_below_trigger():
    store = MemoryChatStore()
    conv = _seed(store, 3)  # 3 <= trigger(4)
    llm = CapturingLLM()
    assert _mem(store, llm).maybe_summarize(conv, store.messages(conv.id), prev_len=1) is None
    assert llm.prompts == []


def test_first_summary_folds_all_older_than_window():
    store = MemoryChatStore()
    conv = _seed(store, 6)  # window=2 → 4 messages are older
    llm = CapturingLLM()
    out = _mem(store, llm).maybe_summarize(conv, store.messages(conv.id), prev_len=4)
    assert out == "ROLLED UP"
    assert store.get(conv.id).summary == "ROLLED UP"
    # the folded span was m0..m3 (everything but the last window of 2)
    assert "m0" in llm.prompts[0] and "m3" in llm.prompts[0]
    assert "m5" not in llm.prompts[0]  # still in the verbatim window


def test_summary_folds_incrementally():
    store = MemoryChatStore()
    conv = _seed(store, 8)
    conv = store.update(conv.id, summary="prev")
    llm = CapturingLLM(summary="NEW")
    # prev_len=6 (>trigger) → only the span that newly left the window folds
    _mem(store, llm).maybe_summarize(conv, store.messages(conv.id), prev_len=6)
    prompt = llm.prompts[0]
    assert "prev" in prompt          # builds on the prior summary
    assert "m4" in prompt and "m5" in prompt   # newly departed pair
    assert "m0" not in prompt        # already summarized earlier — not re-folded


def test_summarize_disabled_is_noop():
    store = MemoryChatStore()
    conv = _seed(store, 10)
    llm = CapturingLLM()
    assert _mem(store, llm, summarize=False).maybe_summarize(
        conv, store.messages(conv.id), prev_len=8) is None
    assert llm.prompts == []
