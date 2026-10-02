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


# ── blueprint-driven assessments ─────────────────────────────────────────────
def test_question_types_catalogue_drives_the_builder(client):
    body = client.get("/artifacts/question-types").json()
    types = {t["type"]: t for t in body["types"]}
    assert {"mcq", "true_false", "fill_blank", "matching", "short_answer",
            "long_answer", "numerical", "ordering"} <= set(types)
    assert types["long_answer"]["retrieval"] == "explain"
    assert {s["value"] for s in body["scopes"]} == {"auto", "focused", "section", "chapter"}
    assert body["presets"] and body["limits"]["max_total"] > 0


def test_retrieval_plan_endpoint_previews_the_strategy(client):
    spec = {"types": [{"type": "mcq", "count": 6}, {"type": "long_answer", "count": 2}]}
    body = client.post("/artifacts/retrieval-plan", json=spec).json()
    assert [(p["profile"], p["granularity"]) for p in body["passes"]] == [
        ("facts", "narrow"), ("explain", "section")]
    assert body["total_questions"] == 8
    assert client.post("/artifacts/retrieval-plan", json={"types": []}).status_code == 422


def test_generate_quiz_with_a_blueprint_returns_a_conformed_editable_ir():
    response = {**_QUIZ_RESPONSE, "questions": [
        {"id": "a", "type": "short_answer", "prompt": "Q?", "answer": "x", "objective_ids": ["O1"]},
        {"id": "b", "type": "short_answer", "prompt": "Q2?", "answer": "y", "objective_ids": ["O1"]},
    ]}
    c = _client(response)
    spec = {"types": [{"type": "short_answer", "count": 1, "difficulty": "hard", "marks_each": 2}]}
    r = c.post("/artifacts/quiz/generate", json={"topic": "Sound", "grade": 7, "subject": "Science",
                                                  "spec": spec})
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["questions"]) == 1 and body["questions"][0]["difficulty"] == "hard"
    assert body["spec"]["types"][0]["marks_each"] == 2
    # the IR round-trips straight into export (md/docx/pptx)
    for fmt in ("md", "docx", "pptx"):
        assert c.post(f"/artifacts/quiz/export?fmt={fmt}", json=body).status_code == 200


def test_invalid_blueprint_is_rejected_and_slides_have_no_blueprint(client):
    bad = {"topic": "Sound", "grade": 7, "subject": "Science",
           "spec": {"types": [{"type": "mcq", "count": 0}]}}
    assert client.post("/artifacts/quiz/generate", json=bad).status_code == 422
    ok = {"topic": "Sound", "grade": 7, "subject": "Science",
          "spec": {"types": [{"type": "mcq", "count": 1}]}}
    r = client.post("/artifacts/slides/generate", json=ok)
    assert r.status_code == 422 and "blueprint" in r.json()["detail"]
