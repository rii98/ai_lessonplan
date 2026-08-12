"""API surface for the refine step: /lessons/refine and /lessons/refine/targets."""

from __future__ import annotations

import json
import re

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


class SectionLLM(FakeLLM):
    def __init__(self, sections) -> None:
        super().__init__(response={})
        self._sections = sections

    def complete(self, prompt, *, system=None, json_schema=None, temperature=None) -> LLMResult:
        m = _TARGET_RE.search(prompt)
        value = self._sections[m.group(1)] if m else self._sections["*"]
        payload = value if m is None else {"section": value}
        return LLMResult(text=json.dumps(payload), raw={})


def _client(llm) -> TestClient:
    embedder, store, reranker = FakeEmbedder(), FakeVectorStore(), FakeReranker()
    retriever = Retriever(embedder=embedder, vector_store=store, reranker=reranker)
    settings = Settings(
        llm={"provider": "ollama", "model": "gemma4:31b-cloud"},
        embedding={"provider": "fastembed", "model": "BAAI/bge-small-en-v1.5"},
        reranker={"provider": "noop"},
        vector_store={"provider": "qdrant"},
    )
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
        intake=HeuristicIntake(),
    )
    app = create_app()
    app.dependency_overrides[get_container] = lambda: container
    return TestClient(app)


def test_refine_targets_lists_sections_and_coupling():
    resp = _client(FakeLLM()).get("/lessons/refine/targets")
    assert resp.status_code == 200
    body = resp.json()
    assert body["whole_document"] == "*"
    by_name = {s["target"]: s for s in body["sections"]}
    assert by_name["objectives"]["coupled"] == ["phases", "formative_checks"]
    assert by_name["local_context"]["leaf"] is True


def test_refine_endpoint_proposes_a_candidate(valid_ldd_dict):
    new_hook = {"prompt": "The river floods every monsoon — why does that happen?",
                "kind": "scenario"}
    client = _client(SectionLLM({"engagement_hook": new_hook}))
    resp = client.post(
        "/lessons/refine",
        json={"ldd": valid_ldd_dict, "target": "engagement_hook",
              "instruction": "use the local river"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["applied"] is False
    assert body["candidate"]["engagement_hook"]["kind"] == "scenario"
    assert list(body["diff"]) == ["engagement_hook"]


def test_refine_endpoint_rejects_with_reason(valid_ldd_dict):
    bad_hook = {"prompt": "Definition: environment is everything around us",
                "kind": "question"}
    client = _client(SectionLLM({"engagement_hook": bad_hook}))
    resp = client.post(
        "/lessons/refine",
        json={"ldd": valid_ldd_dict, "target": "engagement_hook", "instruction": "define"},
    )
    assert resp.status_code == 200  # a rejection is a normal result, not an error
    body = resp.json()
    assert body["ok"] is False
    assert body["candidate"] is None
    assert body["errors"]


def test_refine_endpoint_unknown_target_is_422(valid_ldd_dict):
    resp = _client(FakeLLM()).post(
        "/lessons/refine",
        json={"ldd": valid_ldd_dict, "target": "topic", "instruction": "x"},
    )
    assert resp.status_code == 422


def test_refine_endpoint_requires_instruction(valid_ldd_dict):
    resp = _client(FakeLLM()).post(
        "/lessons/refine",
        json={"ldd": valid_ldd_dict, "target": "materials", "instruction": ""},
    )
    assert resp.status_code == 422  # pydantic min_length on instruction
