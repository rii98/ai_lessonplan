"""ChatPipeline end-to-end with fakes: events stream in order, the turn is
persisted with citations, and a fresh conversation is auto-titled."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from lessonforge.config import ChatConfig
from lessonforge.domain.chat import ChatMessageRequest, Conversation, FilterMode, Role
from lessonforge.providers.base import LLMClient, LLMResult, VectorRecord
from lessonforge.rag.documents import COLLECTION_KEY, SOURCE_KEY, TEXT_KEY
from lessonforge.rag.retriever import Retriever
from lessonforge.services.chat.context import ContextBuilder
from lessonforge.services.chat.memory import ConversationMemory
from lessonforge.services.chat.pipeline import ChatPipeline
from lessonforge.services.chat.retrieve import ChatRetriever
from lessonforge.services.chat.store import MemoryChatStore
from lessonforge.services.chat.synthesize import AnswerSynthesizer
from lessonforge.services.chat.transform import PassthroughTransformer


class StreamLLM(LLMClient):
    def __init__(self, tokens):
        self.tokens = tokens

    def complete(self, prompt, *, system=None, json_schema=None, temperature=None) -> LLMResult:
        return LLMResult(text="".join(self.tokens))

    def stream(self, prompt, *, system=None, temperature=None) -> Iterator[str]:
        yield from self.tokens

    def health(self) -> bool:
        return True


@pytest.fixture
def pipeline(fake_embedder, fake_store, fake_reranker):
    fake_store.ensure_collection("curriculum", 8)
    fake_store.upsert("curriculum", [
        VectorRecord(id="c1", vector=[0.1] * 8, payload={
            TEXT_KEY: "photosynthesis converts light into chemical energy",
            SOURCE_KEY: "CDC Science 6", COLLECTION_KEY: "curriculum", "grade": 6,
        }),
    ])
    retriever = Retriever(embedder=fake_embedder, vector_store=fake_store,
                          reranker=fake_reranker, hybrid=False)
    config = ChatConfig()
    config.retrieval.collections = ["curriculum"]
    store = MemoryChatStore()
    llm = StreamLLM(["Photosynthesis", " makes energy ", "[1]."])
    return ChatPipeline(
        store=store,
        memory=ConversationMemory(store, config.memory, llm=llm),
        transformer=PassthroughTransformer(),
        retriever=ChatRetriever(retriever, config.retrieval),
        context_builder=ContextBuilder(config.context),
        synthesizer=AnswerSynthesizer(llm, config.synthesis),
        config=config,
    ), store


def test_stream_emits_tokens_sources_done(pipeline):
    pipe, store = pipeline
    conv = store.create(Conversation())
    events = list(pipe.stream(conv, ChatMessageRequest(message="what is photosynthesis?")))

    types = [e.type for e in events]
    assert types[0] == "token"
    assert "sources" in types and types[-1] == "done"

    answer = "".join(e.data for e in events if e.type == "token")
    assert answer == "Photosynthesis makes energy [1]."

    sources = next(e for e in events if e.type == "sources").data
    assert sources and sources[0]["source"] == "CDC Science 6"
    assert sources[0]["text"]  # chunk text carried for preview


def test_turn_is_persisted_with_citations(pipeline):
    pipe, store = pipeline
    conv = store.create(Conversation())
    list(pipe.stream(conv, ChatMessageRequest(message="what is photosynthesis?")))

    msgs = store.messages(conv.id)
    assert [m.role for m in msgs] == [Role.user, Role.assistant]
    assert msgs[1].content == "Photosynthesis makes energy [1]."
    assert msgs[1].citations and msgs[1].citations[0].collection == "curriculum"


def test_first_message_titles_the_conversation(pipeline):
    pipe, store = pipeline
    conv = store.create(Conversation())  # title defaults to "New chat"
    done = list(pipe.stream(conv, ChatMessageRequest(message="Explain the water cycle")))[-1]
    assert done.data["title"] == "Explain the water cycle"
    assert store.get(conv.id).title == "Explain the water cycle"


def test_broad_mode_override_is_recorded(pipeline):
    pipe, store = pipeline
    conv = store.create(Conversation())
    list(pipe.stream(conv, ChatMessageRequest(message="q", mode=FilterMode.broad)))
    user_msg = store.messages(conv.id)[0]
    assert user_msg.meta["mode"] == "broad"
