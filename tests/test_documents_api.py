"""Document persistence API (Phase 3): generate-and-save, list, revisit a
version, refine-with-commit, undo/redo, delete — the HTTP surface of the memory
layer. Runs against the in-memory document store via the test container."""

from __future__ import annotations

import json
import re

import pytest
from fastapi.testclient import TestClient

from lessonforge.api.deps import get_container
from lessonforge.api.main import create_app
from lessonforge.config import Settings
from lessonforge.container import Container
from lessonforge.export import ExportService
from lessonforge.providers.base import LLMResult
from lessonforge.rag.grounding import GroundingRetriever
from lessonforge.rag.retriever import Retriever
from lessonforge.services.generation import LessonGenerator
from lessonforge.services.intake import HeuristicIntake
from tests.conftest import FakeEmbedder, FakeLLM, FakeReranker, FakeVectorStore

_TARGET_RE = re.compile(r'Improve ONLY the "([^"]+)" section')


@pytest.fixture
def client(base_settings_dict, valid_ldd_dict) -> TestClient:
    class DualLLM(FakeLLM):
        """Whole-LDD for generation; the matching section envelope for a refine —
        so both the generate and the refine-commit paths succeed with one fake."""

        def complete(self, prompt, *, system=None, json_schema=None, temperature=None):
            m = _TARGET_RE.search(prompt)
            if m:
                return LLMResult(text=json.dumps({"section": valid_ldd_dict[m.group(1)]}))
            return LLMResult(text=json.dumps(valid_ldd_dict))

    llm = DualLLM(response=valid_ldd_dict)
    embedder, store, reranker = FakeEmbedder(), FakeVectorStore(), FakeReranker()
    retriever = Retriever(embedder=embedder, vector_store=store, reranker=reranker)
    settings = Settings(**base_settings_dict)
    grounding = GroundingRetriever(retriever, settings.grounding)
    container = Container(
        settings=settings, llm=llm, embedder=embedder, reranker=reranker,
        vector_store=store, retriever=retriever, grounding=grounding,
        generator=LessonGenerator(llm=llm, grounding=grounding),
        exporter=ExportService(settings.export),
        intake=HeuristicIntake(),
    )
    app = create_app()
    app.dependency_overrides[get_container] = lambda: container
    return TestClient(app)


def _create(client, **kw) -> dict:
    body = {"topic": "Photosynthesis", "grade": 10, "subject": "Science", **kw}
    resp = client.post("/documents/lessons", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_generate_and_save_creates_a_document(client):
    saved = _create(client, owner_id="t1", title="My Lesson")
    assert saved["document"]["title"] == "My Lesson"
    assert saved["document"]["owner_id"] == "t1"
    assert saved["document"]["head_version_id"] == saved["version_id"]
    assert saved["ldd"]["topic"]


def test_list_documents_by_owner(client):
    _create(client, owner_id="t1")
    _create(client, owner_id="t2")
    ours = client.get("/documents", params={"owner_id": "t1"}).json()
    assert len(ours) == 1 and ours[0]["owner_id"] == "t1"


def test_get_document_returns_head_content(client):
    saved = _create(client)
    doc_id = saved["document"]["id"]
    view = client.get(f"/documents/{doc_id}").json()
    assert view["document"]["id"] == doc_id
    assert view["ldd"]["topic"] == saved["ldd"]["topic"]


def test_get_unknown_document_is_404(client):
    assert client.get("/documents/ghost").status_code == 404


def test_versions_start_with_one_generate(client):
    saved = _create(client)
    versions = client.get(f"/documents/{saved['document']['id']}/versions").json()
    assert len(versions) == 1
    assert versions[0]["origin"] == "generate"


def test_refine_commit_appends_version_and_undo_redo(client):
    saved = _create(client)
    doc_id = saved["document"]["id"]
    # refine + commit → a second version
    resp = client.post(
        f"/documents/{doc_id}/refine",
        json={"target": "materials", "instruction": "use lab equipment", "commit": True},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["result"]["ok"] is True
    assert body["version_id"] is not None
    versions = client.get(f"/documents/{doc_id}/versions").json()
    assert len(versions) == 2 and versions[1]["origin"] == "refine"

    # undo → head returns to the first version
    undone = client.post(f"/documents/{doc_id}/undo").json()
    assert undone["head_version_id"] == versions[0]["id"]
    # redo → forward again
    redone = client.post(f"/documents/{doc_id}/redo").json()
    assert redone["head_version_id"] == versions[1]["id"]


def test_refine_without_commit_only_proposes(client):
    saved = _create(client)
    doc_id = saved["document"]["id"]
    resp = client.post(
        f"/documents/{doc_id}/refine",
        json={"target": "materials", "instruction": "use lab equipment"},
    )
    assert resp.status_code == 200
    assert resp.json()["version_id"] is None  # not committed
    assert len(client.get(f"/documents/{doc_id}/versions").json()) == 1  # still one


def test_undo_at_root_is_409(client):
    saved = _create(client)
    assert client.post(f"/documents/{saved['document']['id']}/undo").status_code == 409


def test_delete_document(client):
    saved = _create(client)
    doc_id = saved["document"]["id"]
    assert client.delete(f"/documents/{doc_id}").json() == {"deleted": True}
    assert client.get(f"/documents/{doc_id}").status_code == 404


def test_manual_commit_appends_version_and_persists_edit(client):
    saved = _create(client)
    doc_id = saved["document"]["id"]
    edited = dict(saved["ldd"])
    edited["topic"] = "Photosynthesis (hand-edited)"
    resp = client.post(f"/documents/{doc_id}/manual", json=edited)
    assert resp.status_code == 200, resp.text
    assert resp.json()["version_id"] != saved["version_id"]
    versions = client.get(f"/documents/{doc_id}/versions").json()
    assert len(versions) == 2 and versions[1]["origin"] == "manual"
    # the head now serves the edited content, and undo restores the original
    assert client.get(f"/documents/{doc_id}").json()["ldd"]["topic"] == "Photosynthesis (hand-edited)"
    client.post(f"/documents/{doc_id}/undo")
    assert client.get(f"/documents/{doc_id}").json()["ldd"]["topic"] == saved["ldd"]["topic"]


def test_manual_commit_unknown_document_is_404(client, valid_ldd_dict):
    assert client.post("/documents/ghost/manual", json=valid_ldd_dict).status_code == 404
