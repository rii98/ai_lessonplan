"""Shared test fixtures and in-memory fakes.

The fakes implement the provider interfaces so the whole pipeline runs in-process
with no Qdrant, no Ollama, no model downloads — this is what "loosely coupled"
buys us at test time.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from lessonforge.auth.principal import Perm, Principal
from lessonforge.config import Settings
from lessonforge.knowledge.scope import GLOBAL_SPACE, SPACE_KEY, AccessScope
from lessonforge.providers.base import (
    Embedder,
    LLMClient,
    LLMResult,
    Reranker,
    RerankResult,
    ScoredRecord,
    SparseEmbedder,
    SparseVector,
    StoredRecord,
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


def _in_scope(record: VectorRecord, scope: AccessScope) -> bool:
    # a point written before spaces existed (no _space) is shared curriculum
    return scope.allows(record.payload.get(SPACE_KEY, GLOBAL_SPACE))


class FakeVectorStore(VectorStore):
    """In-memory store honouring the port's contract — including knowledge-space
    isolation: every read and delete is limited to ``scope``."""

    def __init__(self) -> None:
        self.data: dict[str, list[VectorRecord]] = {}

    def ensure_collection(self, name: str, dim: int, *, sparse: bool = False) -> None:
        self.data.setdefault(name, [])

    def upsert(self, name: str, records: list[VectorRecord]) -> None:
        bucket = self.data.setdefault(name, [])
        ids = {r.id for r in records}
        bucket[:] = [r for r in bucket if r.id not in ids] + list(records)  # upsert semantics

    def _visible(self, name: str, scope: AccessScope, where=None) -> list[VectorRecord]:
        recs = [r for r in self.data.get(name, []) if _in_scope(r, scope)]
        if where:
            recs = [r for r in recs if all(r.payload.get(k) == v for k, v in where.items())]
        return recs

    def search(self, name, vector, top_k, where=None, *, scope) -> list[ScoredRecord]:
        recs = self._visible(name, scope, where)
        scored = [ScoredRecord(id=r.id, score=1.0 - i * 0.01, payload=r.payload)
                  for i, r in enumerate(recs)]
        return scored[:top_k]

    def count(self, name: str, *, scope) -> int:
        return len(self._visible(name, scope))

    def scroll(self, name, *, limit, offset=None, scope):
        recs = self._visible(name, scope)
        start = int(offset) if offset else 0
        page = recs[start:start + limit]
        out = [StoredRecord(id=r.id, payload=r.payload) for r in page]
        nxt = str(start + limit) if start + limit < len(recs) else None
        return out, nxt

    def delete(self, name: str, ids: list[str], *, scope) -> int:
        recs = self.data.get(name)
        if not recs:
            return 0
        drop = set(ids)
        self.data[name] = [r for r in recs if not (r.id in drop and _in_scope(r, scope))]
        return len(recs) - len(self.data[name])

    def delete_where(self, name, where, *, scope) -> None:
        self.data[name] = [r for r in self.data.get(name, [])
                           if not (_in_scope(r, scope)
                                   and all(r.payload.get(k) == v for k, v in where.items()))]

    def backfill_space(self, name: str, space: str) -> int:
        n = 0
        for r in self.data.get(name, []):
            if SPACE_KEY not in r.payload:
                r.payload[SPACE_KEY] = space
                n += 1
        return n

    def health(self) -> bool:
        return True


class FakeSparseEmbedder(SparseEmbedder):
    """Deterministic sparse vectors from token hashes — no model, no download."""

    def embed_sparse(self, texts: list[str]) -> list[SparseVector]:
        out = []
        for t in texts:
            toks = sorted({abs(hash(w)) % 1000 for w in t.lower().split()})
            out.append(SparseVector(indices=toks, values=[1.0] * len(toks)))
        return out


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
def fake_sparse_embedder() -> FakeSparseEmbedder:
    return FakeSparseEmbedder()


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
def valid_ldd(valid_ldd_dict):
    from lessonforge.domain.ldd import LessonDesignDocument
    return LessonDesignDocument.model_validate(valid_ldd_dict)


@pytest.fixture
def weak_ldd_dict() -> dict[str, Any]:
    """A structurally VALID but pedagogically generic LDD: bland hook, no
    misconceptions, no local context, thin activities, no homework. Passes the
    LDD guardrails yet scores low on the rubric — the reviser's raw material."""
    return {
        "topic": "Plants",
        "curriculum_ref": {"board": "CDC", "grade": 6, "subject": "Science"},
        "duration_min": 45,
        "objectives": [
            {"id": "O1", "statement": "Learn about plants today", "bloom": "remember"}
        ],
        "misconceptions": [],
        "engagement_hook": {"prompt": "Let us learn about plants.", "kind": "question"},
        "local_context": [],
        "phases": [
            {"name_en": "Engage", "teacher_activities": ["Explain"],
             "student_activities": ["Listen"], "minutes": 45, "objective_ids": ["O1"]}
        ],
        "materials": [],
        "formative_checks": [
            {"id": "Q1", "type": "short_answer", "prompt": "What is a plant?",
             "answer": "a living thing", "objective_ids": ["O1"]}
        ],
    }


@pytest.fixture
def weak_ldd(weak_ldd_dict):
    from lessonforge.domain.ldd import LessonDesignDocument
    return LessonDesignDocument.model_validate(weak_ldd_dict)


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


# ── principals & authenticated API clients ───────────────────────────────────
_ROLE_PERMS = {
    "student": frozenset({Perm.CHAT}),
    "teacher": frozenset({Perm.CHAT, Perm.AUTHOR}),
    "service": frozenset({Perm.REPORTS_READ}),
    "superadmin": frozenset(Perm),
}


def make_principal(role: str = "teacher", *, tenant: str | None = "sch-1",
                   subject: str | None = None, grade: int | None = None,
                   name: str | None = None, superadmin: bool = False) -> Principal:
    """A principal as the auth service would build it (no token involved)."""
    import uuid

    roles = frozenset({role, "superadmin"} if superadmin else {role})
    perms = frozenset().union(*(_ROLE_PERMS.get(r, frozenset()) for r in roles))
    sub = subject or f"{role}:{uuid.uuid4().hex[:8]}"
    return Principal(id=str(uuid.uuid4()), issuer="edboox", subject=sub, role=role, roles=roles,
                     permissions=perms, tenant_id=tenant, display_name=name or sub, grade=grade)


SUPERADMIN = make_principal("teacher", subject="teacher:1", superadmin=True, name="Owner")


def api_client(container, principal: Principal | None = SUPERADMIN, **kw):
    """A TestClient over the real app with the container and the authenticated
    principal overridden — the ONLY way tests bypass token verification (there is
    no auth-off switch in production code). ``principal=None`` leaves auth real."""
    from fastapi.testclient import TestClient

    from lessonforge.api.deps import authenticate, get_container
    from lessonforge.api.main import create_app

    app = create_app()
    app.dependency_overrides[get_container] = lambda: container
    if principal is not None:
        app.dependency_overrides[authenticate] = lambda: principal
    return TestClient(app, **kw)


def as_principal(client, principal: Principal):
    """Switch an api_client's caller (role-matrix / IDOR tests)."""
    from lessonforge.api.deps import authenticate

    client.app.dependency_overrides[authenticate] = lambda: principal
    return client


# ── real Postgres (contract tests) ───────────────────────────────────────────
@pytest.fixture(scope="session")
def pg_database():
    """A fresh, fully-migrated database for the session, or skip.

    Set ``TEST_DATABASE_URL`` (a server the tests may CREATE DATABASE on), e.g.
    ``postgresql://lessonforge:lessonforge@localhost:55432/lessonforge``. CI runs a
    Postgres service; locally ``docker compose up -d postgres``."""
    import os
    import uuid

    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set — Postgres contract tests skipped")
    import psycopg
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    from lessonforge.config import DatabaseConfig
    from lessonforge.db.migrate import migrate
    from lessonforge.db.pool import Database

    name = f"lf_test_{uuid.uuid4().hex[:10]}"
    try:
        admin = psycopg.connect(url, autocommit=True)
    except psycopg.OperationalError as exc:
        pytest.skip(f"Postgres unreachable: {exc}")
    admin.execute(f'CREATE DATABASE "{name}"')
    test_url = make_conninfo(**{**conninfo_to_dict(url), "dbname": name})
    with psycopg.connect(test_url, autocommit=True) as conn:
        migrate(conn, log=lambda *_: None)
    db = Database(DatabaseConfig(provider="postgres", url=test_url, pool_max=4))
    yield db
    db.close()
    admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    admin.close()


@pytest.fixture
def pg(pg_database):
    """The session database, emptied before each test (fast: one TRUNCATE)."""
    with pg_database.tx() as cur:
        cur.execute("TRUNCATE principals, used_token_ids, audit_log, corpus_sources, "
                    "activity_events, chat_conversations, documents RESTART IDENTITY CASCADE")
        cur.execute("DELETE FROM knowledge_spaces WHERE id <> 'global'")
    return pg_database


def pg_principal(db, role: str = "teacher", *, tenant: str | None = "sch-1",
                 name: str | None = None, **kw) -> Principal:
    """A principal that also exists in the ``principals`` table (FK targets)."""
    import uuid

    from lessonforge.auth.directory import PostgresPrincipalDirectory
    from lessonforge.auth.verifier import VerifiedToken

    sub = kw.pop("subject", None) or f"{role}:{uuid.uuid4().hex[:8]}"
    rec = PostgresPrincipalDirectory(db).upsert(VerifiedToken(
        issuer="edboox", subject=sub, role=role, tenant_id=tenant, display_name=name or sub,
        grade=None, jti=None, expires_at=0, issued_at=0))
    base = make_principal(role, tenant=tenant, subject=sub, name=name or sub, **kw)
    from dataclasses import replace

    return replace(base, id=rec.id)



def build_container(settings: Settings, *, llm=None, store=None, **kw):
    """A Container over in-memory fakes (no Qdrant/Ollama/models), wired like
    production — same Platform, same ingestor (vocabulary + knowledge registry)."""
    from lessonforge.container import Container
    from lessonforge.export import ExportService
    from lessonforge.rag.grounding import GroundingRetriever
    from lessonforge.rag.retriever import Retriever
    from lessonforge.services.generation import LessonGenerator
    from lessonforge.services.intake import HeuristicIntake

    llm = llm or FakeLLM()
    embedder, reranker = FakeEmbedder(), FakeReranker()
    store = store if store is not None else FakeVectorStore()
    retriever = Retriever(embedder=embedder, vector_store=store, reranker=reranker)
    grounding = GroundingRetriever(retriever, settings.grounding)
    return Container(settings=settings, llm=llm, embedder=embedder, reranker=reranker,
                     vector_store=store, retriever=retriever, grounding=grounding,
                     generator=LessonGenerator(llm=llm, grounding=grounding),
                     exporter=ExportService(settings.export), intake=HeuristicIntake(), **kw)
