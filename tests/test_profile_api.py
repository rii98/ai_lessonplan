"""API surface for M4.5: the /profile endpoints, the /lessons/validate guardrail
check the editor calls, the profile's effect on /lessons/generate, and the served
editor page itself."""

from __future__ import annotations

import copy

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
        intake=HeuristicIntake(),  # deterministic; profile_store defaults to memory
    )
    app = create_app()
    app.dependency_overrides[get_container] = lambda: container
    return TestClient(app)


# ── profile CRUD ──────────────────────────────────────────────────────────────
def test_get_profile_returns_empty_default(client):
    body = client.get("/profile").json()
    assert body["owner_id"] == "default"
    assert body["style_notes"] == ""
    assert body["local_anchors"] == []


def test_put_then_get_profile_roundtrips(client):
    payload = {
        "default_grade": 6,
        "default_subject": "Science",
        "style_notes": "warm, storytelling",
        "local_anchors": ["Phewa lake"],
    }
    put = client.put("/profile", json=payload)
    assert put.status_code == 200, put.text
    got = client.get("/profile").json()
    assert got["default_grade"] == 6
    assert got["style_notes"] == "warm, storytelling"
    assert got["local_anchors"] == ["Phewa lake"]


def test_put_profile_rejects_bad_grade(client):
    resp = client.put("/profile", json={"default_grade": 99})
    assert resp.status_code == 422  # ge/le bounds on the model


# ── the profile changes what /generate can do ─────────────────────────────────
def test_generate_needs_grade_without_profile(client):
    # topic only, no grade, empty profile → intake cannot determine grade
    resp = client.post("/lessons/generate", json={"topic": "Environment"})
    assert resp.status_code == 422


def test_generate_uses_profile_default_grade(client):
    client.put("/profile", json={"default_grade": 6, "default_subject": "Science"})
    resp = client.post("/lessons/generate", json={"topic": "Environment"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["objectives"][0]["id"] == "O1"


# ── validate endpoint powers the editor's guardrail check ─────────────────────
def test_validate_accepts_a_good_ldd(client, valid_ldd_dict):
    body = client.post("/lessons/validate", json=valid_ldd_dict).json()
    assert body["valid"] is True
    assert body["errors"] == []


def test_validate_flags_an_unassessed_objective(client, valid_ldd_dict):
    broken = copy.deepcopy(valid_ldd_dict)
    broken["formative_checks"] = []  # O1 now taught but never assessed
    resp = client.post("/lessons/validate", json=broken)
    assert resp.status_code == 200  # a normal editing state, not an HTTP error
    body = resp.json()
    assert body["valid"] is False
    assert any("formative check" in e["msg"] or "assessed" in e["msg"] for e in body["errors"])


def test_validate_flags_a_definition_hook(client, valid_ldd_dict):
    broken = copy.deepcopy(valid_ldd_dict)
    broken["engagement_hook"]["prompt"] = "Definition: the environment is everything around us"
    body = client.post("/lessons/validate", json=broken).json()
    assert body["valid"] is False
    assert any("engagement_hook" in e["loc"] for e in body["errors"])


# ── the editor page is served ─────────────────────────────────────────────────
def test_index_serves_the_editor(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "LessonForge" in resp.text
    assert "/lessons/generate" in resp.text  # the page wires the real endpoints
