"""API tests with the Container overridden by in-memory fakes (no services)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from lessonforge.api.deps import get_container
from lessonforge.api.main import create_app
from lessonforge.config import Settings
from lessonforge.container import Container
from lessonforge.export import ExportService
from lessonforge.rag.grounding import GroundingRetriever
from lessonforge.rag.retriever import Retriever
from lessonforge.services.generation import LessonGenerator
from tests.conftest import FakeEmbedder, FakeLLM, FakeReranker, FakeVectorStore


@pytest.fixture
def client(base_settings_dict, valid_ldd_dict) -> TestClient:
    llm = FakeLLM(response=valid_ldd_dict)
    embedder, store, reranker = FakeEmbedder(), FakeVectorStore(), FakeReranker()
    retriever = Retriever(embedder=embedder, vector_store=store, reranker=reranker)
    settings = Settings(**base_settings_dict)
    grounding = GroundingRetriever(retriever, settings.grounding)
    container = Container(
        settings=settings,
        llm=llm,
        embedder=embedder,
        reranker=reranker,
        vector_store=store,
        retriever=retriever,
        grounding=grounding,
        generator=LessonGenerator(llm=llm, grounding=grounding),
        exporter=ExportService(settings.export),
    )
    app = create_app()
    app.dependency_overrides[get_container] = lambda: container
    return TestClient(app)


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_provider_health_reports_wiring(client):
    resp = client.get("/health/providers")
    assert resp.status_code == 200
    body = resp.json()
    assert body["llm"]["provider"] == "ollama"
    assert body["llm"]["model"] == "gemma4:31b-cloud"
    assert body["reranker"]["provider"] == "noop"


def test_generate_lesson_endpoint(client):
    resp = client.post(
        "/lessons/generate",
        json={"topic": "Environment", "grade": 6, "subject": "Science", "duration_min": 45},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["objectives"][0]["id"] == "O1"
    assert body["engagement_hook"]["kind"] == "question"


def test_generate_validates_input(client):
    resp = client.post("/lessons/generate", json={"topic": "x", "grade": 99, "subject": "S"})
    assert resp.status_code == 422  # grade out of range
