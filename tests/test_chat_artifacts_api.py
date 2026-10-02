"""The student journey over HTTP: ask for a quiz in chat (SSE) → it appears as an
artifact → open it (answer-free) → play with server grading → finish → review →
retry what was missed → revisit later → export. Wired with fakes; no Ollama/Qdrant."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from lessonforge.api.deps import get_container
from lessonforge.api.main import create_app
from lessonforge.config import Settings
from lessonforge.container import Container
from lessonforge.export import ExportService
from lessonforge.providers.base import LLMClient, LLMResult
from lessonforge.rag.grounding import GroundingRetriever
from lessonforge.rag.retriever import Retriever
from lessonforge.services.generation import LessonGenerator
from lessonforge.services.intake import HeuristicIntake
from tests.conftest import FakeEmbedder, FakeReranker, FakeVectorStore

QUIZ = {"topic": "Sound", "curriculum_ref": {"board": "CDC", "grade": 8, "subject": "General"},
        "objectives": [{"id": "O1", "statement": "Explain that sound is a vibration", "bloom": "understand"}],
        "questions": [
            {"id": "a", "type": "mcq", "prompt": "What makes sound?", "options": ["stillness", "vibration"],
             "answer": "vibration", "objective_ids": ["O1"], "explanation": "Vibrations make sound."},
            {"id": "b", "type": "mcq", "prompt": "Sound needs?", "options": ["a medium", "nothing"],
             "answer": "a medium", "objective_ids": ["O1"], "explanation": "It needs a medium."},
            {"id": "c", "type": "mcq", "prompt": "Loudness depends on?", "options": ["amplitude", "colour"],
             "answer": "amplitude", "objective_ids": ["O1"], "explanation": "Bigger vibrations are louder."},
        ]}


class AppLLM(LLMClient):
    def complete(self, prompt, *, system=None, json_schema=None, temperature=None):
        if system and "intent router" in system:
            return LLMResult(text=json.dumps({"intent": "quiz", "confidence": 0.95,
                                              "slots": {"topic": "sound", "count": 3, "types": ["mcq"]}}), raw={})
        return LLMResult(text=json.dumps(QUIZ), raw={})

    def stream(self, prompt, *, system=None, temperature=None):
        yield "A plain answer."

    def health(self):
        return True


@pytest.fixture
def client(base_settings_dict) -> TestClient:
    llm = AppLLM()
    emb, store, rr = FakeEmbedder(), FakeVectorStore(), FakeReranker()
    retr = Retriever(embedder=emb, vector_store=store, reranker=rr)
    settings = Settings(**{**base_settings_dict, "chat": {
        "query_transform": {"provider": "passthrough"}, "retrieval": {"collections": []},
        "quiz": {"llm_grading": False}}})
    grounding = GroundingRetriever(retr, settings.grounding)
    c = Container(settings=settings, llm=llm, llm_fast=llm, embedder=emb, reranker=rr,
                  vector_store=store, retriever=retr, grounding=grounding,
                  generator=LessonGenerator(llm=llm, grounding=grounding),
                  exporter=ExportService(settings.export), intake=HeuristicIntake())
    app = create_app()
    app.dependency_overrides[get_container] = lambda: c
    return TestClient(app)


def _sse(text):
    out = []
    for frame in text.strip().split("\n\n"):
        ev, data = "message", ""
        for line in frame.split("\n"):
            if line.startswith("event:"):
                ev = line[6:].strip()
            elif line.startswith("data:"):
                data += line[5:].strip()
        out.append((ev, json.loads(data) if data else None))
    return out


def _chat(client, cid, text):
    return _sse(client.post(f"/chat/conversations/{cid}/message", json={"message": text}).text)


@pytest.fixture
def quiz(client):
    cid = client.post("/chat/conversations", json={}).json()["id"]
    events = _chat(client, cid, "quiz me on sound")
    art = next(d for e, d in events if e == "artifact")
    return cid, art["id"], events


def test_asking_for_a_quiz_streams_intent_status_artifact_then_text(quiz):
    _, _aid, events = quiz
    assert [e for e, _ in events] == ["intent", "status", "artifact", "token", "done"]
    assert dict(events)["intent"]["skill"] == "quiz"
    assert dict(events)["artifact"]["question_count"] == 3


def test_a_plain_question_in_the_same_chat_is_still_a_normal_answer(client, quiz):
    cid, *_ = quiz
    events = _chat(client, cid, "explain photosynthesis")
    assert [e for e, _ in events] == ["token", "sources", "done"]


def test_the_quiz_is_listed_with_the_conversation_for_revisiting(client, quiz):
    cid, aid, _ = quiz
    body = client.get(f"/chat/conversations/{cid}").json()
    assert [a["id"] for a in body["artifacts"]] == [aid]
    reply = next(m for m in body["messages"] if m["role"] == "assistant")
    assert reply["meta"]["artifact_ids"] == [aid]


def test_opening_a_quiz_exposes_no_answers(client, quiz):
    _, aid, _ = quiz
    body = client.get(f"/chat/artifacts/{aid}").json()
    blob = json.dumps(body["play"])
    assert "Vibrations make sound" not in blob and '"answer"' not in blob
    assert [q["widget"] for q in body["play"]["questions"]] == ["choice"] * 3
    assert body["attempts"] == []


def test_full_play_through_review_retry_and_revisit(client, quiz):
    cid, aid, _ = quiz
    play = client.get(f"/chat/artifacts/{aid}").json()["play"]
    qids = [q["id"] for q in play["questions"]]
    att = client.post(f"/chat/artifacts/{aid}/attempts").json()["attempt_id"]

    ok = client.post(f"/chat/attempts/{att}/answer", json={"question_id": qids[0], "response": 1}).json()
    assert ok["correct"] and ok["expected"] == "vibration" and ok["explanation"] == "Vibrations make sound."
    bad = client.post(f"/chat/attempts/{att}/answer", json={"question_id": qids[1], "response": 1}).json()
    assert bad["correct"] is False and bad["expected"] == "a medium"
    # idempotent: re-sending the right answer doesn't rewrite history
    again = client.post(f"/chat/attempts/{att}/answer", json={"question_id": qids[1], "response": 0}).json()
    assert again["correct"] is False

    review = client.post(f"/chat/attempts/{att}/finish").json()
    assert review["attempt"]["status"] == "completed" and review["attempt"]["score"] == 1.0
    assert review["missed"] == qids[1:]

    retry = client.post(f"/chat/attempts/{att}/retry-missed").json()
    assert retry["question_count"] == 2 and retry["title"].startswith("Retry:")
    assert len(client.get(f"/chat/conversations/{cid}").json()["artifacts"]) == 2   # multiple quizzes

    # revisit later: the history lists the attempt, and its review is still there
    detail = client.get(f"/chat/artifacts/{aid}").json()
    assert [a["id"] for a in detail["attempts"]] == [att] and detail["summary"]["best"] == 1.0
    assert client.get(f"/chat/attempts/{att}").json()["questions"][1]["expected"] == "a medium"

    # a second attempt is independent
    att2 = client.post(f"/chat/artifacts/{aid}/attempts").json()["attempt_id"]
    assert att2 != att and client.get(f"/chat/attempts/{att2}").json()["attempt"]["score"] == 0


def test_nothing_missed_means_no_retry(client, quiz):
    _, aid, _ = quiz
    play = client.get(f"/chat/artifacts/{aid}").json()["play"]
    att = client.post(f"/chat/artifacts/{aid}/attempts").json()["attempt_id"]
    client.post(f"/chat/attempts/{att}/answer", json={"question_id": play["questions"][0]["id"], "response": 1})
    # q1 right, the rest unanswered → finish counts them missed; but a perfect run has none
    for q, r in zip(play["questions"][1:], (0, 0), strict=True):
        client.post(f"/chat/attempts/{att}/answer", json={"question_id": q["id"], "response": r})
    client.post(f"/chat/attempts/{att}/finish")
    assert client.post(f"/chat/attempts/{att}/retry-missed").status_code == 422


def test_export_the_quiz_for_printing(client, quiz):
    _, aid, _ = quiz
    for fmt, magic in (("docx", b"PK"), ("pptx", b"PK"), ("md", b"# Quiz")):
        r = client.post(f"/chat/artifacts/{aid}/export?fmt={fmt}")
        assert r.status_code == 200 and r.content.startswith(magic), fmt
    assert client.post(f"/chat/artifacts/{aid}/export?fmt=xyz").status_code == 422


def test_deleting_the_conversation_removes_its_quizzes(client, quiz):
    cid, aid, _ = quiz
    att = client.post(f"/chat/artifacts/{aid}/attempts").json()["attempt_id"]
    client.delete(f"/chat/conversations/{cid}")
    assert client.get(f"/chat/artifacts/{aid}").status_code == 404
    assert client.get(f"/chat/attempts/{att}").status_code == 404


def test_deleting_one_quiz_keeps_the_conversation(client, quiz):
    cid, aid, _ = quiz
    assert client.delete(f"/chat/artifacts/{aid}").json() == {"deleted": True}
    assert client.get(f"/chat/conversations/{cid}").json()["artifacts"] == []


def test_errors(client, quiz):
    _, aid, _ = quiz
    assert client.get("/chat/artifacts/nope").status_code == 404
    assert client.post("/chat/artifacts/nope/attempts").status_code == 404
    assert client.post("/chat/attempts/nope/answer", json={"question_id": "x"}).status_code == 404
    att = client.post(f"/chat/artifacts/{aid}/attempts").json()["attempt_id"]
    assert client.post(f"/chat/attempts/{att}/answer", json={"response": 1}).status_code == 422
    assert client.post(f"/chat/attempts/{att}/answer", json={"question_id": "zzz", "response": 1}).status_code == 422


def test_chat_page_links_and_serves_the_canvas_assets(client):
    page = client.get("/chat").text
    assert "/static/chat_artifacts.js" in page and "/static/chat_artifacts.css" in page
    assert 'id="canvas"' in page
    js = client.get("/static/chat_artifacts.js")
    assert js.status_code == 200 and "RENDERERS" in js.text      # the kind → renderer registry
    assert client.get("/static/chat_artifacts.css").status_code == 200
