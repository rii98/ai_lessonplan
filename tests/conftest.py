"""Shared test fixtures and in-memory fakes.

The fakes implement the provider interfaces so the whole pipeline runs in-process
with no Qdrant, no Ollama, no model downloads — this is what "loosely coupled"
buys us at test time.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from lessonforge.config import Settings
from lessonforge.providers.base import (
    Embedder,
    LLMClient,
    LLMResult,
    Reranker,
    RerankResult,
    ScoredRecord,
    VectorRecord,
    VectorStore,
)


class FakeLLM(LLMClient):
    def __init__(self, response: dict[str, Any] | None = None) -> None:
        self._response = response or {}
        self.calls: list[dict[str, Any]] = []

    def complete(self, prompt, *, system=None, json_schema=None, temperature=None) -> LLMResult:
        self.calls.append({"prompt": prompt, "system": system, "schema": json_schema})
        return LLMResult(text=json.dumps(self._response), raw={})

    def health(self) -> bool:
        return True


class FakeEmbedder(Embedder):
    def __init__(self, dim: int = 8) -> None:
        self._dim = dim

    @property
    def dim(self) -> int:
        return self._dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        # deterministic pseudo-embedding from string hash
        out = []
        for t in texts:
            h = abs(hash(t))
            out.append([((h >> i) & 0xFF) / 255.0 for i in range(self._dim)])
        return out


class FakeVectorStore(VectorStore):
    def __init__(self) -> None:
        self.data: dict[str, list[VectorRecord]] = {}

    def ensure_collection(self, name: str, dim: int) -> None:
        self.data.setdefault(name, [])

    def upsert(self, name: str, records: list[VectorRecord]) -> None:
        self.data.setdefault(name, []).extend(records)

    def search(self, name, vector, top_k, where=None) -> list[ScoredRecord]:
        recs = self.data.get(name, [])
        scored = [ScoredRecord(id=r.id, score=1.0 - i * 0.01, payload=r.payload)
                  for i, r in enumerate(recs)]
        return scored[:top_k]

    def health(self) -> bool:
        return True


class FakeReranker(Reranker):
    def rerank(self, query, documents, top_n) -> list[RerankResult]:
        # rank by keyword overlap with the query (deterministic, no model)
        q = set(query.lower().split())
        scored = [
            RerankResult(index=i, score=len(q & set(d.lower().split())), document=d)
            for i, d in enumerate(documents)
        ]
        scored.sort(key=lambda r: r.score, reverse=True)
        return scored[:top_n]


@pytest.fixture
def fake_embedder() -> FakeEmbedder:
    return FakeEmbedder()


@pytest.fixture
def fake_store() -> FakeVectorStore:
    return FakeVectorStore()


@pytest.fixture
def fake_reranker() -> FakeReranker:
    return FakeReranker()


@pytest.fixture
def valid_ldd_dict() -> dict[str, Any]:
    """A minimal LDD that passes every structural guardrail."""
    return {
        "topic": "Components of Environment: Biotic and Abiotic",
        "curriculum_ref": {"board": "CDC", "grade": 6, "subject": "Science"},
        "duration_min": 45,
        "language": "en-ne",
        "framework": "5E",
        "objectives": [
            {"id": "O1", "statement": "Classify components into biotic and abiotic",
             "bloom": "understand"}
        ],
        "prior_knowledge": ["Living vs non-living things"],
        "misconceptions": [
            {"statement": "Clouds move so they are living",
             "correction": "Movement is not life; clouds are abiotic"}
        ],
        "engagement_hook": {
            "prompt": "What did you see on your way to school today?",
            "kind": "question",
        },
        "local_context": ["paddy field", "goat", "mushroom", "river"],
        "phases": [
            {
                "name_en": "Engage",
                "name_ne": "संलग्न गराउनु",
                "teacher_activities": ["Ask what students saw on the way to school"],
                "student_activities": ["Share one observation"],
                "minutes": 5,
                "objective_ids": ["O1"],
            }
        ],
        "materials": ["Component cards", "Board and marker"],
        "formative_checks": [
            {
                "id": "Q1",
                "type": "short_answer",
                "prompt": "Name one biotic and one abiotic component",
                "answer": "biotic: goat; abiotic: soil",
                "objective_ids": ["O1"],
            }
        ],
        "homework": {"instructions": ["Draw four things and label B/A"], "objective_ids": ["O1"]},
    }


@pytest.fixture
def export_ldd_dict() -> dict[str, Any]:
    """A fuller LDD that exercises every renderer branch: two objectives, all
    three question types, bilingual phases, differentiation, grounding sources,
    and phase minutes that sum exactly to the target duration (balanced timing).
    Kept fixed so the golden-file tests are stable."""
    return {
        "topic": "Components of Environment: Biotic and Abiotic",
        "curriculum_ref": {"board": "CDC", "grade": 6, "subject": "Science", "code": "SC6.2.1"},
        "duration_min": 45,
        "language": "en-ne",
        "framework": "5E",
        "objectives": [
            {"id": "O1", "statement": "Classify components into biotic and abiotic",
             "bloom": "understand"},
            {"id": "O2", "statement": "Identify producers, consumers, and decomposers",
             "bloom": "apply"},
        ],
        "prior_knowledge": ["Living vs non-living things", "Examples from surroundings"],
        "misconceptions": [
            {"statement": "Clouds move so they are living",
             "correction": "Movement is not life; clouds are abiotic",
             "source": "CDC Science 6, misconception bank"},
        ],
        "engagement_hook": {
            "prompt": "What did you see on your way to school today?",
            "kind": "question",
        },
        "local_context": ["paddy field", "goat", "mushroom", "river water", "sunlight"],
        "phases": [
            {"name_en": "Engage", "name_ne": "संलग्न गराउनु",
             "teacher_activities": ["Ask what students saw on the way to school"],
             "student_activities": ["Share one observation"],
             "minutes": 10, "objective_ids": ["O1"]},
            {"name_en": "Explore", "name_ne": "अन्वेषण गर्नु",
             "teacher_activities": ["Give groups component cards to sort"],
             "student_activities": ["Sort cards and justify choices"],
             "minutes": 15, "objective_ids": ["O1", "O2"]},
            {"name_en": "Explain", "name_ne": "व्याख्या गर्नु",
             "teacher_activities": ["Introduce producers, consumers, decomposers"],
             "student_activities": ["Copy the concept map"],
             "minutes": 20, "objective_ids": ["O2"]},
        ],
        "materials": ["Component cards", "Board and marker", "Textbook"],
        "differentiation": {
            "struggling": ["Provide labelled picture cards"],
            "on_level": ["Sort mixed cards"],
            "advanced": ["Build a food chain from the cards"],
        },
        "formative_checks": [
            {"id": "Q1", "type": "mcq",
             "prompt": "Which of these is a biotic component?",
             "answer": "goat", "objective_ids": ["O1"],
             "options": ["soil", "goat", "river water", "sunlight"]},
            {"id": "Q2", "type": "true_false",
             "prompt": "A mushroom is a decomposer.",
             "answer": "True", "objective_ids": ["O2"], "options": None},
            {"id": "Q3", "type": "short_answer",
             "prompt": "Name one producer found near your school.",
             "answer": "rice plant", "objective_ids": ["O1", "O2"], "options": None},
        ],
        "homework": {"instructions": ["Draw four things and label B/A",
                                      "Mark producers, consumers, decomposers"],
                     "objective_ids": ["O1", "O2"]},
        "quality": {"grounding_sources": ["CDC Science Grade 6, Unit 2"], "notes": ""},
    }


@pytest.fixture
def export_ldd(export_ldd_dict):
    from lessonforge.domain.ldd import LessonDesignDocument
    return LessonDesignDocument.model_validate(export_ldd_dict)


@pytest.fixture
def base_settings_dict() -> dict[str, Any]:
    return {
        "llm": {"provider": "ollama", "model": "gemma4:31b-cloud"},
        "embedding": {"provider": "fastembed", "model": "BAAI/bge-small-en-v1.5"},
        "reranker": {"provider": "noop"},
        "vector_store": {"provider": "qdrant"},
    }


@pytest.fixture
def settings(base_settings_dict) -> Settings:
    return Settings(**base_settings_dict)
