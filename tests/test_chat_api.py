"""Chat API surface: conversation CRUD and the SSE message stream, wired through
a fake container (no Qdrant, no Ollama)."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from lessonforge.api.deps import get_container
from lessonforge.api.main import create_app
from lessonforge.config import Settings
from lessonforge.container import Container
from lessonforge.export import ExportService
from lessonforge.providers.base import VectorRecord
from lessonforge.rag.documents import COLLECTION_KEY, SOURCE_KEY, TEXT_KEY
from lessonforge.rag.grounding import GroundingRetriever
from lessonforge.rag.retriever import Retriever
from lessonforge.services.generation import LessonGenerator
from lessonforge.services.intake import HeuristicIntake
from tests.conftest import FakeEmbedder, FakeLLM, FakeReranker, FakeVectorStore


@pytest.fixture
def client(base_settings_dict) -> TestClient:
    llm = FakeLLM(response={"answer": "ok"})
    embedder, store, reranker = FakeEmbedder(), FakeVectorStore(), FakeReranker()
    store.ensure_collection("curriculum", 8)
    store.upsert("curriculum", [VectorRecord(id="c1", vector=[0.1] * 8, payload={
        TEXT_KEY: "photosynthesis converts light energy", SOURCE_KEY: "CDC 6",
        COLLECTION_KEY: "curriculum", "grade": 6,
    })])
    retriever = Retriever(embedder=embedder, vector_store=store, reranker=reranker)
    # passthrough query transform keeps the fake LLM out of query-understanding
    settings = Settings(**{**base_settings_dict,
                           "chat": {"query_transform": {"provider": "passthrough"},
                                    "retrieval": {"collections": ["curriculum"]}}})
    grounding = GroundingRetriever(retriever, settings.grounding)
    container = Container(
        settings=settings, llm=llm, embedder=embedder, reranker=reranker,
        vector_store=store, retriever=retriever, grounding=grounding,
        generator=LessonGenerator(llm=llm, grounding=grounding),
        exporter=ExportService(settings.export), intake=HeuristicIntake(),
    )
    app = create_app()
    app.dependency_overrides[get_container] = lambda: container
    return TestClient(app)


def _parse_sse(text):
    events = []
    for frame in text.strip().split("\n\n"):
        if not frame.strip():
            continue
        ev, data = "message", ""
        for line in frame.split("\n"):
            if line.startswith("event:"):
                ev = line[6:].strip()
            elif line.startswith("data:"):
                data += line[5:].strip()
        events.append((ev, json.loads(data) if data else None))
    return events


def test_config_endpoint(client):
    body = client.get("/chat/config").json()
    assert body["collections"] == ["curriculum"]
    assert "grade" in body["filter_fields"]
    assert body["default_mode"] == "scoped"


def test_conversation_crud(client):
    created = client.post("/chat/conversations", json={
        "title": "My chat", "defaults": {"mode": "scoped", "grade": 6, "class": "6A"},
    }).json()
    cid = created["id"]
    assert created["defaults"]["class"] == "6A"

    assert any(c["id"] == cid for c in client.get("/chat/conversations").json())

    got = client.get(f"/chat/conversations/{cid}").json()
    assert got["conversation"]["title"] == "My chat"
    assert got["messages"] == []

    renamed = client.patch(f"/chat/conversations/{cid}", json={"title": "Renamed"}).json()
    assert renamed["title"] == "Renamed"

    assert client.delete(f"/chat/conversations/{cid}").json() == {"deleted": True}
    assert client.get(f"/chat/conversations/{cid}").status_code == 404


def test_message_streams_sse_and_persists(client):
    cid = client.post("/chat/conversations", json={}).json()["id"]
    resp = client.post(f"/chat/conversations/{cid}/message",
                       json={"message": "what is photosynthesis?", "mode": "broad"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")

    events = _parse_sse(resp.text)
    types = [e for e, _ in events]
    assert "token" in types and "sources" in types
    assert types[-1] == "done"

    # the turn was persisted
    msgs = client.get(f"/chat/conversations/{cid}").json()["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant"]


def test_message_on_missing_conversation_is_404(client):
    assert client.post("/chat/conversations/nope/message",
                       json={"message": "hi"}).status_code == 404


def test_chat_page_served(client):
    r = client.get("/chat")
    assert r.status_code == 200 and "LessonForge" in r.text
