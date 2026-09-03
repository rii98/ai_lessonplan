"""ContextBuilder: numbering, budget packing, and compression."""

from __future__ import annotations

from lessonforge.config import ChatContextConfig
from lessonforge.providers.base import LLMClient, LLMResult
from lessonforge.services.chat.context import ContextBuilder
from lessonforge.services.chat.retrieve import ScoredChunk


def _chunk(text, src="s", col="reference", **meta):
    return ScoredChunk(text=text, source=src, collection=col, score=1.0, metadata=meta)


def test_numbers_citations_and_embeds_markers():
    ctx = ContextBuilder(ChatContextConfig(max_chars=6000))
    built = ctx.build("q", [_chunk("first", grade=6), _chunk("second")])
    assert [c.n for c in built.citations] == [1, 2]
    assert "[1]" in built.text and "[2]" in built.text
    assert built.citations[0].metadata == {"grade": 6}


def test_budget_truncates_but_keeps_at_least_one():
    ctx = ContextBuilder(ChatContextConfig(max_chars=10))  # tiny budget
    built = ctx.build("q", [_chunk("A" * 50), _chunk("B" * 50)])
    assert len(built.citations) == 1  # first always included, second dropped
    # numbering stays contiguous with what was actually included
    assert built.citations[0].n == 1


def test_empty_input_yields_empty_context():
    built = ContextBuilder(ChatContextConfig()).build("q", [])
    assert built.is_empty and built.text == ""


class _CompressLLM(LLMClient):
    def complete(self, prompt, *, system=None, json_schema=None, temperature=None) -> LLMResult:
        return LLMResult(text="KEPT")

    def health(self) -> bool:
        return True


class _EmptyLLM(LLMClient):
    def complete(self, prompt, *, system=None, json_schema=None, temperature=None) -> LLMResult:
        return LLMResult(text="")

    def health(self) -> bool:
        return True


def test_compression_replaces_text_when_enabled():
    ctx = ContextBuilder(ChatContextConfig(compression=True), llm=_CompressLLM())
    built = ctx.build("q", [_chunk("long original passage")])
    assert built.citations[0].text == "KEPT"


def test_compression_keeps_original_when_it_drops_everything():
    ctx = ContextBuilder(ChatContextConfig(compression=True), llm=_EmptyLLM())
    built = ctx.build("q", [_chunk("keep me")])
    assert built.citations[0].text == "keep me"
