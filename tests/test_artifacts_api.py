"""Targeted-artifact API: generate ONE artifact from a brief (editable IR back),
then export that IR to a file. Wired with fakes — no external services."""

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

_QUIZ_RESPONSE = {
    "topic": "Sound and Vibration",
    "curriculum_ref": {"board": "CDC", "grade": 7, "subject": "Science", "code": None},
    "objectives": [{"id": "O1", "statement": "Explain that sound is a vibration",
                    "bloom": "understand"}],
    "questions": [{"id": "Q1", "type": "short_answer", "prompt": "What causes sound?",
                   "answer": "vibration", "objective_ids": ["O1"], "options": None}],
    "instructions": "Answer all.",
}


def _client(response: dict) -> TestClient:
    llm = FakeLLM(response=response)
    embedder, store, reranker = FakeEmbedder(), FakeVectorStore(), FakeReranker()
    retriever = Retriever(embedder=embedder, vector_store=store, reranker=reranker)
    settings = Settings(llm={"provider": "ollama", "model": "m"},
                        embedding={"provider": "fastembed", "model": "e"},
                        reranker={"provider": "noop"}, vector_store={"provider": "qdrant"})
    grounding = GroundingRetriever(retriever, settings.grounding)
    container = Container(
        settings=settings, llm=llm, embedder=embedder, reranker=reranker,
        vector_store=store, retriever=retriever, grounding=grounding,
        generator=LessonGenerator(llm=llm, grounding=grounding),
        exporter=ExportService(settings.export),
    )
    app = create_app()
    app.dependency_overrides[get_container] = lambda: container
    return TestClient(app)


@pytest.fixture
def client() -> TestClient:
    return _client(_QUIZ_RESPONSE)


def test_manifest_lists_generatable_artifacts(client):
    body = client.get("/artifacts/manifest").json()
    kinds = {g["kind"] for g in body["generatable"]}
    assert kinds == {"quiz", "worksheet", "slides"}
    quiz = next(g for g in body["generatable"] if g["kind"] == "quiz")
    assert "docx" in quiz["formats"] and "md" in quiz["formats"]


def test_generate_quiz_returns_editable_ir(client):
    resp = client.post("/artifacts/quiz/generate", json={"topic": "Sound", "grade": 7,
                                                          "subject": "Science"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["topic"] == "Sound and Vibration"
    assert body["questions"][0]["id"] == "Q1"


def test_generate_then_export_roundtrip(client):
    ir = client.post("/artifacts/quiz/generate",
                     json={"topic": "Sound", "grade": 7, "subject": "Science"}).json()
    resp = client.post("/artifacts/quiz/export?fmt=md", json=ir)
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/markdown")
    assert "# Quiz — Sound and Vibration" in resp.content.decode()


def test_export_validates_the_ir_shape(client):
    resp = client.post("/artifacts/quiz/export", json={"topic": "x"})  # missing questions
    assert resp.status_code == 422


def test_lesson_plan_is_not_generatable_standalone(client):
    resp = client.post("/artifacts/lesson_plan/generate",
                       json={"topic": "Sound", "grade": 7, "subject": "Science"})
    assert resp.status_code == 422
    assert "not generatable" in resp.json()["detail"]


def test_refine_targets_endpoint(client):
    body = client.get("/artifacts/quiz/refine/targets").json()
    targets = {s["target"] for s in body["sections"]}
    assert {"objectives", "questions", "instructions"} <= targets
    instr = next(s for s in body["sections"] if s["target"] == "instructions")
    assert instr["grounded"] is False  # cosmetic → no re-grounding


def test_refine_artifact_roundtrip():
    # a section-shaped LLM reply so the reprompt of "instructions" succeeds
    c = _client({"section": "Read each question carefully before answering."})
    quiz = {
        "topic": "Sound", "curriculum_ref": {"board": "CDC", "grade": 7, "subject": "Science"},
        "objectives": [{"id": "O1", "statement": "explain sound is a vibration", "bloom": "understand"}],
        "questions": [{"id": "Q1", "type": "short_answer", "prompt": "cause?",
                       "answer": "vibration", "objective_ids": ["O1"]}],
        "instructions": "Answer.",
    }
    resp = c.post("/artifacts/quiz/refine",
                  json={"artifact": quiz, "target": "instructions", "instruction": "clearer"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] and body["candidate"]["instructions"].startswith("Read each")
    assert set(body["diff"]) == {"instructions"}


def test_refine_rejects_missing_instruction(client):
    resp = client.post("/artifacts/quiz/refine", json={"artifact": {}, "target": "questions"})
    assert resp.status_code == 422


def test_generate_worksheet_and_slides():
    ws_resp = {
        "topic": "Sound", "curriculum_ref": {"board": "CDC", "grade": 7, "subject": "Science"},
        "objectives": [{"id": "O1", "statement": "Explain that sound is a vibration",
                        "bloom": "understand"}],
        "tasks": ["Hum and feel the buzz."],
        "questions": [],
    }
    c = _client(ws_resp)
    r = c.post("/artifacts/worksheet/generate", json={"topic": "Sound", "grade": 7,
                                                      "subject": "Science"})
    assert r.status_code == 200, r.text
    assert r.json()["tasks"] == ["Hum and feel the buzz."]
