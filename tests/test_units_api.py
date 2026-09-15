"""Unit API (Phase 4): plan, generate-and-persist, revisit, regenerate one day,
and export — the HTTP surface of multi-day units over the in-memory stores."""

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
from lessonforge.services.critique import StructuralCritic
from lessonforge.services.generation import LessonGenerator
from lessonforge.services.revise import Reviser
from tests.conftest import FakeEmbedder, FakeReranker, FakeVectorStore
from tests.test_unit_generation import UnitLLM


@pytest.fixture
def client(base_settings_dict) -> TestClient:
    llm = UnitLLM()
    embedder, store, reranker = FakeEmbedder(), FakeVectorStore(), FakeReranker()
    retriever = Retriever(embedder=embedder, vector_store=store, reranker=reranker)
    settings = Settings(**base_settings_dict)
    grounding = GroundingRetriever(retriever, settings.grounding)
    container = Container(
        settings=settings, llm=llm, embedder=embedder, reranker=reranker,
        vector_store=store, retriever=retriever, grounding=grounding,
        generator=LessonGenerator(llm=llm, grounding=grounding),
        exporter=ExportService(settings.export),
        # passthrough reviser so day expansion needs no extra fake responses
        reviser=Reviser(llm=None, critic=StructuralCritic()),
    )
    app = create_app()
    app.dependency_overrides[get_container] = lambda: container
    return TestClient(app)


def _req(**kw) -> dict:
    return {"topic": "Scientific Study", "grade": 10, "subject": "Science",
            "num_days": 3, **kw}


def test_plan_returns_a_spine(client):
    resp = client.post("/units/plan", json=_req())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    plan = body["plan"]
    assert len(plan["days"]) == 3
    assert [d["day"] for d in plan["days"]] == [1, 2, 3]
    # the coverage report is present (empty topics when no book is ingested)
    assert "coverage" in body and "topics" in body["coverage"]


def test_generate_persists_a_unit(client):
    resp = client.post("/units/generate", json=_req(owner_id="t1", title="Sci Unit"))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["document"]["title"] == "Sci Unit"
    assert body["document"]["kind"] == "unit"
    assert len(body["unit"]["days"]) == 3
    assert body["document"]["head_version_id"] == body["version_id"]


def test_get_unit_returns_head_content(client):
    doc_id = client.post("/units/generate", json=_req()).json()["document"]["id"]
    view = client.get(f"/units/{doc_id}").json()
    assert view["document"]["id"] == doc_id
    assert len(view["unit"]["days"]) == 3
    assert "coherence" in view["unit"]


def test_unit_shows_up_in_documents_list(client):
    client.post("/units/generate", json=_req(owner_id="t9"))
    docs = client.get("/documents", params={"owner_id": "t9"}).json()
    assert len(docs) == 1 and docs[0]["kind"] == "unit"


def test_regenerate_day_appends_version(client):
    doc_id = client.post("/units/generate", json=_req()).json()["document"]["id"]
    resp = client.post(f"/units/{doc_id}/days/2/regenerate")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["unit"]["days"]) == 3
    versions = client.get(f"/documents/{doc_id}/versions").json()
    assert len(versions) == 2 and versions[1]["instruction"] == "regenerated day 2"
    # undo brings back the pre-regeneration unit
    client.post(f"/documents/{doc_id}/undo")
    assert len(client.get(f"/units/{doc_id}").json()["unit"]["days"]) == 3


def test_regenerate_out_of_range_day_is_422(client):
    doc_id = client.post("/units/generate", json=_req()).json()["document"]["id"]
    assert client.post(f"/units/{doc_id}/days/99/regenerate").status_code == 422


def test_get_unknown_unit_is_404(client):
    assert client.get("/units/ghost").status_code == 404


def test_export_unit_returns_a_zip(client):
    doc_id = client.post("/units/generate", json=_req()).json()["document"]["id"]
    resp = client.post(f"/units/{doc_id}/export")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    assert resp.content[:2] == b"PK"  # zip magic


def test_generate_with_edited_plan_expands_that_arc(client):
    # a teacher-edited spine must drive generation, not a fresh re-plan
    plan = client.post("/units/plan", json=_req()).json()["plan"]
    plan["days"][0]["topic"] = "Teacher-Chosen Opening Topic"
    resp = client.post("/units/generate", json=_req(plan=plan))
    assert resp.status_code == 200, resp.text
    unit = resp.json()["unit"]
    assert unit["days"][0]["topic"] == "Teacher-Chosen Opening Topic"


def test_edit_day_commits_a_manual_version(client):
    doc_id = client.post("/units/generate", json=_req()).json()["document"]["id"]
    unit = client.get(f"/units/{doc_id}").json()["unit"]
    edited = dict(unit["days"][1])
    edited["topic"] = "Hand-edited Day Two"
    resp = client.put(f"/units/{doc_id}/days/2", json=edited)
    assert resp.status_code == 200, resp.text
    assert resp.json()["unit"]["days"][1]["topic"] == "Hand-edited Day Two"
    versions = client.get(f"/documents/{doc_id}/versions").json()
    assert len(versions) == 2 and versions[1]["instruction"] == "edited day 2"
    assert versions[1]["origin"] == "manual"
    # undo restores the pre-edit day
    client.post(f"/documents/{doc_id}/undo")
    assert client.get(f"/units/{doc_id}").json()["unit"]["days"][1]["topic"] != "Hand-edited Day Two"


def test_edit_day_out_of_range_is_422(client):
    doc_id = client.post("/units/generate", json=_req()).json()["document"]["id"]
    day = client.get(f"/units/{doc_id}").json()["unit"]["days"][0]
    assert client.put(f"/units/{doc_id}/days/99", json=day).status_code == 422


def test_grounding_preview_returns_a_provenance_tree(client):
    r = client.post("/grounding/preview",
                    json={"topic": "Scientific Study", "grade": 10, "subject": "Science"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert {"grounded", "has_authoritative", "sources", "collections"} <= set(body)
    assert isinstance(body["collections"], list)


def test_edit_day_with_mismatched_grade_is_422(client):
    doc_id = client.post("/units/generate", json=_req()).json()["document"]["id"]
    edited = dict(client.get(f"/units/{doc_id}").json()["unit"]["days"][0])
    edited["curriculum_ref"] = {**edited["curriculum_ref"], "grade": 9}  # breaks the unit invariant
    assert client.put(f"/units/{doc_id}/days/1", json=edited).status_code == 422
