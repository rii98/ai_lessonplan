"""KnowledgeRegistry contract (spaces + corpus_sources) on memory AND Postgres,
plus the ingestor writing one record-book row per document (§16)."""

from __future__ import annotations

import pytest

from lessonforge.config import KnowledgeConfig
from lessonforge.knowledge.registry import (
    CorpusSource,
    MemoryKnowledgeRegistry,
    PostgresKnowledgeRegistry,
    space_record,
)
from lessonforge.knowledge.scope import AccessScope, KnowledgePolicy
from lessonforge.rag.documents import Collection, Document
from lessonforge.rag.ingest import Ingestor
from tests.conftest import FakeEmbedder, FakeVectorStore, make_principal, pg_principal


@pytest.fixture(params=["memory", "postgres"])
def env(request):
    if request.param == "memory":
        return MemoryKnowledgeRegistry(), lambda: make_principal().id
    db = request.getfixturevalue("pg")
    return PostgresKnowledgeRegistry(db), lambda: pg_principal(db).id


def _src(space, doc_id, **kw):
    return CorpusSource(space_id=space, collection="reference", doc_id=doc_id, title=doc_id,
                        checksum="c" * 64, chunks=3, **kw)


def test_spaces_global_first_and_created_on_demand(env):
    reg, _ = env
    assert [s.id for s in reg.spaces()] == ["global"]
    reg.ensure_space("school:sch-7")
    reg.ensure_space("school:sch-7")                         # idempotent
    spaces = reg.spaces()
    assert [s.id for s in spaces] == ["global", "school:sch-7"]
    assert spaces[1].kind == "school" and spaces[1].tenant_id == "sch-7"
    with pytest.raises(ValueError):
        space_record("school:../etc")


def test_sources_upsert_list_filter_and_remove(env):
    reg, owner = env
    who = owner()
    reg.ensure_space("school:sch-7")
    reg.record_sources([_src("global", "book-1", ingested_by=who), _src("school:sch-7", "notes")])
    reg.record_sources([CorpusSource(space_id="global", collection="reference", doc_id="book-1",
                                     title="Book One v2", checksum="d" * 64, chunks=9)])
    rows = reg.list_sources(spaces=None, collection=None, limit=10)
    assert {r.doc_id for r in rows} == {"book-1", "notes"}
    book = next(r for r in rows if r.doc_id == "book-1")
    assert (book.title, book.chunks) == ("Book One v2", 9)       # re-ingest updates in place
    assert [r.doc_id for r in reg.list_sources(spaces=["school:sch-7"], collection=None,
                                               limit=10)] == ["notes"]
    assert reg.list_sources(spaces=["global"], collection="pedagogical", limit=10) == []
    assert reg.remove_source("global", "reference", "book-1").doc_id == "book-1"
    assert reg.remove_source("global", "reference", "book-1") is None


def test_ingestor_records_one_row_per_document_with_checksum():
    reg, store = MemoryKnowledgeRegistry(), FakeVectorStore()
    ing = Ingestor(embedder=FakeEmbedder(), vector_store=store, registry=reg)
    ing.ingest_documents(Collection.reference, [
        Document(id="b1", text="para one\n\npara two", source="Book", metadata={"title": "Light"}),
        Document(id="b2", text="other", source="Book 2")], space="school:sch-7", ingested_by="u1")
    rows = {r.doc_id: r for r in reg.list_sources(spaces=None, collection=None, limit=10)}
    assert set(rows) == {"school:sch-7/b1", "school:sch-7/b2"}      # namespaced by space
    assert rows["school:sch-7/b1"].title == "Light" and rows["school:sch-7/b1"].ingested_by == "u1"
    assert len(rows["school:sch-7/b1"].checksum) == 64
    assert {s.id for s in reg.spaces()} == {"global", "school:sch-7"}
    with pytest.raises(ValueError):
        ing.ingest_documents(Collection.reference, [Document(id="x", text="t", source="s")],
                             space="planet:mars")


def test_write_scope_and_can_write():
    policy = KnowledgePolicy(KnowledgeConfig(write={"superadmin": ["*"],
                                                    "teacher": ["school:{tenant}"]}))
    teacher = make_principal("teacher", tenant="sch-7")
    assert policy.write_scope(teacher) == AccessScope.of("school:sch-7")
    assert policy.can_write(teacher, "school:sch-7") and not policy.can_write(teacher, "global")
    assert not policy.can_write(teacher, "school:sch-9")
    assert policy.write_scope(make_principal(superadmin=True)).unrestricted
    assert policy.write_scope(make_principal("student")) == AccessScope(frozenset())
