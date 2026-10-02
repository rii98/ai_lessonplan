"""Assessments built FROM a lesson or unit: the objectives are the lessons' own, every
question maps to them, and the unit variant spreads across days."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from lessonforge.api.deps import get_container
from lessonforge.api.main import create_app
from lessonforge.config import Settings
from lessonforge.container import Container
from lessonforge.domain.assessment import AssessmentSpec, TypeSpec
from lessonforge.domain.ldd import LessonDesignDocument
from lessonforge.domain.unit import DayPlan, UnitDesignDocument, UnitPlan
from lessonforge.export import ExportService
from lessonforge.export.base import ArtifactKind
from lessonforge.providers.base import LLMClient, LLMResult
from lessonforge.rag.grounding import GroundingRetriever
from lessonforge.rag.retriever import Retriever
from lessonforge.services.artifact_generation import build_generator
from lessonforge.services.assessment_scope import scope_from_lesson, scope_from_unit
from lessonforge.services.documents.store import MemoryDocumentStore
from lessonforge.services.generation import LessonGenerator
from tests.conftest import FakeEmbedder, FakeReranker, FakeVectorStore

_SPEC = AssessmentSpec(types=[TypeSpec(type="short_answer", count=3)])


class Echo(LLMClient):
    """Writes ``count`` short answers, ignoring objectives on purpose (id 'WRONG'),
    and records the prompt."""

    def __init__(self, ids=("WRONG",)):
        self.prompts, self.ids = [], ids

    def complete(self, prompt, *, system=None, json_schema=None, temperature=None):
        self.prompts.append(prompt)
        qs = [{"id": f"x{i}", "type": "short_answer", "prompt": f"Q{i}?", "answer": "a",
               "objective_ids": [self.ids[i % len(self.ids)]]} for i in range(3)]
        return LLMResult(text=json.dumps({
            "topic": "t", "curriculum_ref": {"board": "CDC", "grade": 6, "subject": "Science"},
            "objectives": [{"id": "ZZ", "statement": "the model invented this one", "bloom": "apply"}],
            "questions": qs}), raw={})

    def health(self):
        return True


def _day(export_ldd_dict, topic):
    d = json.loads(json.dumps(export_ldd_dict))
    d["topic"] = topic
    return LessonDesignDocument.model_validate(d)


def _unit(export_ldd_dict, n=3) -> UnitDesignDocument:
    days = [_day(export_ldd_dict, f"Topic {i}") for i in range(1, n + 1)]
    ref = {"grade": 6, "subject": "Science"}
    plan = UnitPlan(title="Environment", curriculum_ref=ref,
                    days=[DayPlan(day=i, topic=f"Topic {i}") for i in range(1, n + 1)])
    return UnitDesignDocument(title="Environment", curriculum_ref=ref, big_idea="Living and non-living",
                              plan=plan, days=days)


# ── scope builders ───────────────────────────────────────────────────────────
def test_lesson_scope_reuses_objectives_and_misconceptions(export_ldd):
    brief, scope = scope_from_lesson(export_ldd)
    assert (brief.topic, brief.grade, brief.subject) == (export_ldd.topic, 6, "Science")
    assert [o.id for o in scope.objectives] == ["O1", "O2"]
    assert "Clouds move so they are living" in scope.focus


def test_unit_scope_reids_per_day_and_notes_each_day(export_ldd_dict):
    brief, scope = scope_from_unit(_unit(export_ldd_dict))
    assert brief.topic == "Environment"
    assert [o.id for o in scope.objectives] == ["D1-O1", "D1-O2", "D2-O1", "D2-O2", "D3-O1", "D3-O2"]
    assert scope.by_day == {1: ["D1-O1", "D1-O2"], 2: ["D2-O1", "D2-O2"], 3: ["D3-O1", "D3-O2"]}
    assert "Day 3: Topic 3" in scope.focus and "do not cluster" in scope.focus


def test_unit_scope_can_select_days_and_rejects_bad_ones(export_ldd_dict):
    unit = _unit(export_ldd_dict)
    _, scope = scope_from_unit(unit, [3, 1])
    assert list(scope.by_day) == [1, 3]
    with pytest.raises(ValueError, match="not in this 3-day unit"):
        scope_from_unit(unit, [4])
    with pytest.raises(ValueError, match="at least one day"):
        scope_from_unit(unit, [])


# ── generation against fixed objectives ──────────────────────────────────────
def test_questions_are_tied_to_the_lessons_objectives_not_the_models(export_ldd):
    brief, scope = scope_from_lesson(export_ldd)
    llm = Echo()   # objective ids "WRONG", objectives "ZZ" — both must be overridden
    quiz = build_generator(ArtifactKind.quiz, llm=llm).generate(brief, spec=_SPEC, scope=scope)
    assert [o.id for o in quiz.objectives] == ["O1", "O2"]
    assert [o.statement for o in quiz.objectives] == [o.statement for o in export_ldd.objectives]
    # unknown ids are re-pointed round-robin so coverage stays even
    assert [q.objective_ids for q in quiz.questions] == [["O1"], ["O2"], ["O1"]]
    p = llm.prompts[0]
    assert "FIXED OBJECTIVES" in p and "O1: Classify components" in p and "Clouds move" in p


def test_valid_ids_the_model_chose_are_kept(export_ldd):
    brief, scope = scope_from_lesson(export_ldd)
    quiz = build_generator(ArtifactKind.quiz, llm=Echo(ids=("O2",))).generate(
        brief, spec=_SPEC, scope=scope)
    assert all(q.objective_ids == ["O2"] for q in quiz.questions)


def test_unit_questions_spread_across_days(export_ldd_dict):
    brief, scope = scope_from_unit(_unit(export_ldd_dict))
    quiz = build_generator(ArtifactKind.quiz, llm=Echo()).generate(brief, spec=_SPEC, scope=scope)
    assert {q.objective_ids[0] for q in quiz.questions} == {"D1-O1", "D1-O2", "D2-O1"}


def test_scope_without_a_blueprint_and_slides_are_rejected(export_ldd):
    brief, scope = scope_from_lesson(export_ldd)
    with pytest.raises(ValueError, match="needs a question blueprint"):
        build_generator(ArtifactKind.quiz, llm=Echo()).generate(brief, scope=scope)
    with pytest.raises(ValueError, match="no question blueprint"):
        build_generator(ArtifactKind.slides, llm=Echo()).generate(brief, spec=_SPEC, scope=scope)


# ── API ──────────────────────────────────────────────────────────────────────
@pytest.fixture
def client_and_container(export_ldd_dict):
    llm = Echo()
    emb, store, rr = FakeEmbedder(), FakeVectorStore(), FakeReranker()
    retr = Retriever(embedder=emb, vector_store=store, reranker=rr)
    st = Settings(llm={"provider": "ollama", "model": "m"},
                  embedding={"provider": "fastembed", "model": "e"},
                  reranker={"provider": "noop"}, vector_store={"provider": "qdrant"})
    g = GroundingRetriever(retr, st.grounding)
    c = Container(settings=st, llm=llm, embedder=emb, reranker=rr, vector_store=store,
                  retriever=retr, grounding=g, generator=LessonGenerator(llm=llm, grounding=g),
                  exporter=ExportService(st.export), document_store=MemoryDocumentStore())
    app = create_app()
    app.dependency_overrides[get_container] = lambda: c
    return TestClient(app), c


_SPEC_JSON = {"types": [{"type": "short_answer", "count": 3}]}


def test_from_lesson_endpoint(client_and_container, export_ldd_dict):
    client, _ = client_and_container
    r = client.post("/artifacts/quiz/from-lesson", json={"ldd": export_ldd_dict, "spec": _SPEC_JSON})
    assert r.status_code == 200, r.text
    body = r.json()
    assert [o["id"] for o in body["objectives"]] == ["O1", "O2"]
    assert body["curriculum_ref"]["grade"] == 6 and body["spec"]["types"][0]["count"] == 3
    assert client.post("/artifacts/quiz/export?fmt=pptx", json=body).status_code == 200


def test_from_lesson_rejects_slides_and_bad_input(client_and_container, export_ldd_dict):
    client, _ = client_and_container
    assert client.post("/artifacts/slides/from-lesson",
                       json={"ldd": export_ldd_dict, "spec": _SPEC_JSON}).status_code == 422
    assert client.post("/artifacts/quiz/from-lesson",
                       json={"ldd": {"topic": "x"}, "spec": _SPEC_JSON}).status_code == 422


def test_from_unit_endpoint_all_days_and_selected(client_and_container, export_ldd_dict):
    client, c = client_and_container
    doc, _ = c.get_document_service().save_unit(_unit(export_ldd_dict))
    r = client.post(f"/units/{doc.id}/assessment/quiz", json={"spec": _SPEC_JSON})
    assert r.status_code == 200, r.text
    assert len(r.json()["objectives"]) == 6          # 3 days × 2 objectives
    r2 = client.post(f"/units/{doc.id}/assessment/worksheet",
                     json={"spec": _SPEC_JSON, "days": [2]})
    assert r2.status_code == 200 and [o["id"] for o in r2.json()["objectives"]] == ["D2-O1", "D2-O2"]
    assert client.post(f"/units/{doc.id}/assessment/quiz",
                       json={"spec": _SPEC_JSON, "days": [9]}).status_code == 422
    assert client.post("/units/nope/assessment/quiz", json={"spec": _SPEC_JSON}).status_code == 404
