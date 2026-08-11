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
from lessonforge.services.intake import HeuristicIntake
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
        # deterministic intake so a pasted plan is parsed by regex, not the fake LLM
        intake=HeuristicIntake(),
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


def test_generate_requires_topic_or_plan(client):
    resp = client.post("/lessons/generate", json={"grade": 6, "subject": "Science"})
    assert resp.status_code == 422  # neither topic nor existing_plan


def test_generate_from_pasted_plan_only(client):
    # US-3: no topic — intake parses it from the pasted plan (heuristic default here)
    resp = client.post(
        "/lessons/generate",
        json={"existing_plan": "Grade 6 Science\nTopic: Environment\nread the textbook"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["objectives"][0]["id"] == "O1"


def test_intake_endpoint_previews_brief(client):
    resp = client.post(
        "/lessons/intake",
        json={"existing_plan": "Class 7 Science 40 min\nTopic: Sound and Vibration"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["topic"] == "Sound and Vibration"
    assert body["grade"] == 7 and body["duration_min"] == 45


def test_critique_endpoint_scores_an_ldd(client, export_ldd_dict):
    resp = client.post("/lessons/critique", json=export_ldd_dict)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert 0.0 <= body["overall"] <= 1.0
    assert set(body["scores"]) >= {"engagement", "alignment", "local_relevance"}
