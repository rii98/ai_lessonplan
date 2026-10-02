"""The pipeline with skills: a quiz request becomes an artifact (persisted, linked to
its message, announced as events), a plain question is untouched, multi-turn slot
filling works, and routing failures can never break chat."""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest

from lessonforge.config import ChatConfig
from lessonforge.domain.chat import ChatMessageRequest, Conversation, Role
from lessonforge.providers.base import LLMClient, LLMResult
from lessonforge.rag.retriever import Retriever
from lessonforge.services.chat.artifacts import ArtifactService
from lessonforge.services.chat.context import ContextBuilder
from lessonforge.services.chat.intent import HybridRouter, IntentRouter
from lessonforge.services.chat.memory import ConversationMemory
from lessonforge.services.chat.pipeline import ChatPipeline
from lessonforge.services.chat.quiz_play import RuleGrader
from lessonforge.services.chat.retrieve import ChatRetriever
from lessonforge.services.chat.skills import RoutedIntent, SkillDeps, build_skills
from lessonforge.services.chat.store import MemoryChatStore
from lessonforge.services.chat.synthesize import AnswerSynthesizer
from lessonforge.services.chat.transform import PassthroughTransformer

QUIZ = {"topic": "Sound", "curriculum_ref": {"board": "CDC", "grade": 8, "subject": "General"},
        "objectives": [{"id": "O1", "statement": "Explain that sound is a vibration", "bloom": "understand"}],
        "questions": [{"id": f"x{i}", "type": "mcq", "prompt": f"Q{i}?", "options": ["a", "b"],
                       "answer": "a", "objective_ids": ["O1"], "explanation": "why"} for i in range(3)]}


class RoutedLLM(LLMClient):
    """One fake for every role: classifies, writes the quiz, streams answers."""

    def __init__(self, intent=None):
        self.intent, self.calls = intent, []

    def complete(self, prompt, *, system=None, json_schema=None, temperature=None):
        self.calls.append(system or "")
        if system and "intent router" in system:
            return LLMResult(text=json.dumps(self.intent or {"intent": "qa", "confidence": 1}), raw={})
        return LLMResult(text=json.dumps(QUIZ), raw={})

    def stream(self, prompt, *, system=None, temperature=None) -> Iterator[str]:
        yield "A plain answer."

    def health(self):
        return True


def _build(fake_embedder, fake_store, fake_reranker, llm, *, router=None, gate="always", **intent):
    config = ChatConfig(intent={"gate": gate, **intent})
    config.retrieval.collections = []
    store = MemoryChatStore()
    deps = SkillDeps(llm=llm, llm_fast=llm, grounding=None, config=config)
    skills = build_skills(deps)
    router = router or HybridRouter(config.intent, [s.spec for s in skills.values()], llm)
    retr = Retriever(embedder=fake_embedder, vector_store=fake_store, reranker=fake_reranker, hybrid=False)
    pipe = ChatPipeline(
        store=store, memory=ConversationMemory(store, config.memory, llm=llm),
        transformer=PassthroughTransformer(), retriever=ChatRetriever(retr, config.retrieval),
        context_builder=ContextBuilder(config.context), synthesizer=AnswerSynthesizer(llm, config.synthesis),
        config=config, router=router, skills=skills, artifacts=ArtifactService(store, RuleGrader()))
    conv = store.create(Conversation(title="New chat"))
    return pipe, store, conv


@pytest.fixture
def parts(fake_embedder, fake_store, fake_reranker):
    def make(intent=None, router=None, **cfg):
        llm = RoutedLLM(intent)
        return (llm, *_build(fake_embedder, fake_store, fake_reranker, llm, router=router, **cfg))
    return make


def _say(pipe, conv, text):
    return list(pipe.stream(conv, ChatMessageRequest(message=text)))


def test_quiz_request_becomes_a_persisted_artifact(parts):
    _llm, pipe, store, conv = parts({"intent": "quiz", "confidence": 0.95,
                                    "slots": {"topic": "sound", "count": 3, "types": ["mcq"]}})
    events = _say(pipe, conv, "quiz me on sound")
    assert [e.type for e in events] == ["intent", "status", "artifact", "token", "done"]

    art_ev = next(e for e in events if e.type == "artifact").data
    assert art_ev["kind"] == "quiz" and art_ev["question_count"] == 3 and art_ev["attempts"] == 0

    msgs = store.messages(conv.id)
    assert [m.role for m in msgs] == [Role.user, Role.assistant]
    reply = msgs[1]
    assert "3-question quiz" in reply.content
    assert reply.meta["intent"]["skill"] == "quiz" and reply.meta["intent"]["source"] == "llm"
    assert reply.meta["artifact_ids"] == [art_ev["id"]]
    stored = store.get_artifact(art_ev["id"])
    assert stored.message_id == reply.id and stored.conversation_id == conv.id    # linked to its turn
    assert next(e for e in events if e.type == "done").data["artifacts"] == [art_ev["id"]]
    assert store.get(conv.id).title == "quiz me on sound"                          # still auto-titles


def test_plain_question_flow_is_untouched(parts):
    llm, pipe, store, conv = parts()           # default: every turn is classified…
    events = _say(pipe, conv, "explain photosynthesis")
    assert [e.type for e in events] == ["token", "sources", "done"]      # …but no intent/artifact noise
    assert store.messages(conv.id)[1].meta["intent"]["skill"] == "qa"
    assert any("intent router" in c for c in llm.calls)


def test_lexical_gate_keeps_the_llm_out_of_plain_questions(parts):
    llm, pipe, _store, conv = parts(gate="lexical")
    _say(pipe, conv, "explain photosynthesis")
    assert not any("intent router" in c for c in llm.calls)


def test_a_paraphrase_with_no_quiz_keywords_still_becomes_a_quiz(parts):
    _, pipe, _store, conv = parts({"intent": "quiz", "confidence": 0.9,
                                  "slots": {"topic": "osmosis", "types": ["mcq"], "count": 3}})
    events = _say(pipe, conv, "help me revise osmosis")
    assert any(e.type == "artifact" for e in events)


# ── routing runs in parallel and can never stall or break the turn ────────────
def test_routing_overlaps_with_retrieval_on_a_worker_thread(parts):
    import threading
    seen = {}
    started, release = threading.Event(), threading.Event()

    class Slow(IntentRouter):
        def route(self, message, state):
            seen["router_thread"] = threading.current_thread().name
            started.set()
            release.wait(2)
            return RoutedIntent()

    class Probe(PassthroughTransformer):
        """Runs during the QA prep: proves the router has ALREADY started (overlap)."""
        def transform(self, question, history):
            seen["router_started_during_prep"] = started.wait(2)
            release.set()
            return super().transform(question, history)

    _, pipe, _store, conv = parts(router=Slow())
    pipe.transformer = Probe()
    _say(pipe, conv, "explain photosynthesis")
    assert seen["router_started_during_prep"] is True
    assert seen["router_thread"].startswith("intent") and seen["router_thread"] != threading.current_thread().name


def test_a_slow_router_times_out_and_the_turn_is_a_plain_answer(parts):
    import time

    class Hang(IntentRouter):
        def route(self, message, state):
            time.sleep(0.6)
            return RoutedIntent("quiz", 1.0, {"topic": "x"})

    _, pipe, _store, conv = parts(router=Hang(), timeout_s=0.05)
    assert [e.type for e in _say(pipe, conv, "quiz me on x")] == ["token", "sources", "done"]


def test_a_skill_turn_survives_a_failing_qa_preparation(parts):
    _llm, pipe, _store, conv = parts({"intent": "quiz", "confidence": 0.9,
                                    "slots": {"topic": "sound", "types": ["mcq"], "count": 3}})
    pipe.retriever.retrieve = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("vector store down"))
    events = _say(pipe, conv, "quiz me on sound")          # retrieval is irrelevant to a quiz
    assert any(e.type == "artifact" for e in events)


def test_a_plain_turn_still_surfaces_a_failing_qa_preparation(parts):
    _, pipe, _store, conv = parts()
    pipe.retriever.retrieve = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("vector store down"))
    with pytest.raises(RuntimeError, match="vector store down"):
        _say(pipe, conv, "explain photosynthesis")


def test_missing_topic_asks_then_the_next_message_completes_the_quiz(parts):
    llm, pipe, store, conv = parts()
    llm.intent = {"intent": "quiz", "confidence": 0.9, "slots": {"count": 3}}
    first = _say(pipe, conv, "quiz me")
    assert [e.type for e in first] == ["intent", "token", "done"] and "What topic" in first[1].data
    assert store.messages(conv.id)[1].meta["awaiting"]["slot"] == "topic"

    second = _say(pipe, conv, "photosynthesis")                          # a short reply: the topic
    assert next(e for e in second if e.type == "intent").data["source"] == "awaiting"
    assert any(e.type == "artifact" for e in second)
    reply = store.messages(conv.id)[-1]
    assert "awaiting" not in reply.meta and reply.meta["artifact_ids"]
    assert store.list_artifacts(conv.id)[0].meta["slots"]["count"] == 3  # earlier slot survived the turn


def test_cancelling_an_awaited_request_falls_back_to_a_normal_answer(parts):
    _llm, pipe, _store, conv = parts({"intent": "quiz", "confidence": 0.9, "slots": {}})
    _say(pipe, conv, "quiz me")
    events = _say(pipe, conv, "never mind")
    assert [e.type for e in events] == ["token", "sources", "done"]


def test_a_skill_that_raises_keeps_the_turn(parts, monkeypatch):
    _llm, pipe, store, conv = parts({"intent": "quiz", "confidence": 0.9, "slots": {"topic": "x"}})
    monkeypatch.setattr(type(pipe.skills["quiz"]), "run",
                        lambda self, ctx: (_ for _ in ()).throw(RuntimeError("boom")))
    events = _say(pipe, conv, "quiz me on x")
    assert [e.type for e in events][-2:] == ["error", "done"]
    assert [m.role for m in store.messages(conv.id)] == [Role.user, Role.assistant]


def test_a_router_that_raises_never_breaks_chat(parts):
    class Boom(IntentRouter):
        def route(self, message, state):
            raise RuntimeError("router bug")

    _, pipe, _store, conv = parts(router=Boom())
    assert [e.type for e in _say(pipe, conv, "quiz me on sound")] == ["token", "sources", "done"]


def test_unknown_skill_from_a_router_is_ignored(parts):
    class Odd(IntentRouter):
        def route(self, message, state):
            return RoutedIntent("telepathy", 1.0)

    _, pipe, _store, conv = parts(router=Odd())
    assert [e.type for e in _say(pipe, conv, "hi")] == ["token", "sources", "done"]


def test_followup_after_a_quiz_routes_back_to_the_quiz_skill(parts):
    _llm, pipe, store, conv = parts({"intent": "quiz", "confidence": 0.9,
                                    "slots": {"topic": "sound", "difficulty": "hard"}})
    _say(pipe, conv, "quiz me on sound")
    events = _say(pipe, conv, "harder")           # no quiz words: only the follow-up cue + last skill
    assert any(e.type == "artifact" for e in events)
    assert len(store.list_artifacts(conv.id)) == 2


def test_the_classifier_is_told_about_quizzes_already_made(parts):
    llm, pipe, _store, conv = parts({"intent": "quiz", "confidence": 0.9, "slots": {"topic": "sound"}})
    seen = []
    orig = llm.complete
    llm.complete = lambda prompt, **kw: (seen.append(prompt), orig(prompt, **kw))[1]
    _say(pipe, conv, "quiz me on sound")
    _say(pipe, conv, "quiz me again on this")
    router_prompts = [p for p in seen if "Latest user message" in p]
    assert "Artifacts already made in this chat" in router_prompts[1] and "Sound quiz" in router_prompts[1]


def test_pipeline_without_a_router_is_exactly_the_old_behaviour(fake_embedder, fake_store, fake_reranker):
    llm = RoutedLLM()
    pipe, store, conv = _build(fake_embedder, fake_store, fake_reranker, llm)
    pipe.router = None
    events = _say(pipe, conv, "quiz me on sound")
    assert [e.type for e in events] == ["token", "sources", "done"]
    assert "intent" not in store.messages(conv.id)[1].meta
