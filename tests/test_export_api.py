"""Export API — posting an LDD returns a downloadable file with the right
content type and filename; the bundle endpoint returns a zip; manifest advertises
what's available. The container is wired with the real ExportService (renderers
are pure, no external services)."""

from __future__ import annotations

import io
import zipfile

import pytest
from fastapi.testclient import TestClient

from lessonforge.api.deps import get_container
from lessonforge.api.main import create_app
from lessonforge.config import Settings
from lessonforge.container import Container
from lessonforge.export import ExportService
from lessonforge.rag.retriever import Retriever
from lessonforge.services.generation import LessonGenerator
from tests.conftest import FakeEmbedder, FakeLLM, FakeReranker, FakeVectorStore


@pytest.fixture
def client(base_settings_dict) -> TestClient:
    llm = FakeLLM(response={})
    embedder, store, reranker = FakeEmbedder(), FakeVectorStore(), FakeReranker()
    retriever = Retriever(embedder=embedder, vector_store=store, reranker=reranker)
    settings = Settings(**base_settings_dict)
    container = Container(
        settings=settings, llm=llm, embedder=embedder, reranker=reranker,
        vector_store=store, retriever=retriever,
        generator=LessonGenerator(llm=llm, retriever=retriever),
        exporter=ExportService(settings.export),
    )
    app = create_app()
    app.dependency_overrides[get_container] = lambda: container
    return TestClient(app)


def test_manifest_endpoint(client):
    resp = client.get("/export/manifest")
    assert resp.status_code == 200
    body = resp.json()
    assert body["artifacts"]["slides"]["default_format"] == "pptx"
    assert "lesson_plan" in body["bundle"]


def test_export_single_artifact_docx(client, export_ldd_dict):
    resp = client.post("/lessons/export/lesson_plan", json=export_ldd_dict)
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    assert "attachment;" in resp.headers["content-disposition"]
    assert resp.headers["content-disposition"].endswith('_lesson_plan.docx"')
    # a real, openable docx
    from docx import Document
    Document(io.BytesIO(resp.content))


def test_export_format_override(client, export_ldd_dict):
    resp = client.post("/lessons/export/lesson_plan?fmt=md", json=export_ldd_dict)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/markdown")
    assert "# Components of Environment" in resp.text


def test_export_slides_pptx(client, export_ldd_dict):
    resp = client.post("/lessons/export/slides", json=export_ldd_dict)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    )
    from pptx import Presentation
    Presentation(io.BytesIO(resp.content))


def test_export_bundle_zip(client, export_ldd_dict):
    resp = client.post("/lessons/export/bundle/zip", json=export_ldd_dict)
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    names = zipfile.ZipFile(io.BytesIO(resp.content)).namelist()
    assert len(names) == 4


def test_unknown_kind_is_422(client, export_ldd_dict):
    resp = client.post("/lessons/export/flashcards", json=export_ldd_dict)
    assert resp.status_code == 422  # not a valid ArtifactKind


def test_unknown_format_is_a_client_error(client, export_ldd_dict):
    """An unsupported format must surface as an error, not a silent empty file."""
    resp = client.post("/lessons/export/slides?fmt=pdf", json=export_ldd_dict)
    assert resp.status_code >= 400


def test_export_rejects_structurally_invalid_ldd(client, export_ldd_dict):
    bad = dict(export_ldd_dict)
    bad["formative_checks"] = []  # objective no longer assessed → LDD invalid
    resp = client.post("/lessons/export/lesson_plan", json=bad)
    assert resp.status_code == 422
