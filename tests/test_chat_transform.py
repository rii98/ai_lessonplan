"""QueryTransformer tests — each advanced technique is independently gated and
degrades safely."""

from __future__ import annotations

import json

from lessonforge.config import ChatQueryTransformConfig
from lessonforge.domain.chat import ChatMessage, Role
from lessonforge.providers.base import LLMClient, LLMResult
from lessonforge.services.chat.transform import (
    LLMTransformer,
    PassthroughTransformer,
    build_query_transformer,
)


class ScriptedLLM(LLMClient):
    """Routes ``complete`` by inspecting the system prompt so one fake can serve
    condensation, expansion, and HyDE with distinct canned outputs."""

    def __init__(self, *, condense="STANDALONE", expansions=None, hyde="HYDE DOC", fail=False):
        self.condense = condense
        self.expansions = expansions if expansions is not None else ["alt one", "alt two"]
        self.hyde = hyde
        self.fail = fail
        self.calls = []

    def complete(self, prompt, *, system=None, json_schema=None, temperature=None) -> LLMResult:
        self.calls.append(system or "")
        if self.fail:
            raise RuntimeError("llm down")
        s = system or ""
        if "standalone question" in s:
            return LLMResult(text=self.condense)
        if "JSON array" in s:
            return LLMResult(text=json.dumps(self.expansions))
        if "textbook-style passage" in s:
            return LLMResult(text=self.hyde)
        return LLMResult(text="")

    def health(self) -> bool:
        return True


def _cfg(**kw):
    base = {"provider": "llm", "condense_history": True, "expansions": 0, "hyde": False}
    base.update(kw)
    return ChatQueryTransformConfig(**base)


def test_passthrough_returns_raw_query():
    t = PassthroughTransformer()
    out = t.transform("what is photosynthesis?", [])
    assert out.queries == ["what is photosynthesis?"]
    assert out.standalone == "what is photosynthesis?"


def test_condense_uses_history():
    llm = ScriptedLLM(condense="What is the Grade 7 photosynthesis outcome?")
    t = LLMTransformer(llm, _cfg(condense_history=True))
    history = [ChatMessage(role=Role.user, content="Tell me about photosynthesis"),
               ChatMessage(role=Role.assistant, content="It is ...")]
    out = t.transform("what about grade 7?", history)
    assert out.standalone == "What is the Grade 7 photosynthesis outcome?"
    assert out.queries[0] == out.standalone


def test_condense_skipped_without_history():
    llm = ScriptedLLM()
    t = LLMTransformer(llm, _cfg(condense_history=True))
    out = t.transform("first question", [])
    assert out.standalone == "first question"  # no history → no LLM condense call
    assert all("standalone" not in c for c in llm.calls)


def test_expansion_adds_queries():
    llm = ScriptedLLM(condense="Q", expansions=["variant a", "variant b", "variant c"])
    t = LLMTransformer(llm, _cfg(expansions=3))
    out = t.transform("Q", [])
    assert "variant a" in out.queries and "variant c" in out.queries
    assert len(out.queries) == 4  # standalone + 3


def test_hyde_appends_hypothetical_doc():
    llm = ScriptedLLM(condense="Q", hyde="A photosynthesis passage.")
    t = LLMTransformer(llm, _cfg(hyde=True))
    out = t.transform("Q", [])
    assert out.hyde_doc == "A photosynthesis passage."
    assert "A photosynthesis passage." in out.queries


def test_all_techniques_degrade_on_llm_failure():
    llm = ScriptedLLM(fail=True)
    t = LLMTransformer(llm, _cfg(expansions=3, hyde=True))
    out = t.transform("resilient?", [ChatMessage(role=Role.user, content="ctx")])
    # everything failed, but retrieval still gets the raw question
    assert out.queries == ["resilient?"]


def test_build_query_transformer_resolves_provider():
    assert isinstance(build_query_transformer(_cfg(provider="passthrough"), llm=ScriptedLLM()),
                      PassthroughTransformer)
    assert isinstance(build_query_transformer(_cfg(provider="llm"), llm=ScriptedLLM()),
                      LLMTransformer)
