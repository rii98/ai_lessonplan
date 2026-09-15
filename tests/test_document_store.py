"""DocumentStore (Phase 3): the memory/sqlite backends persist documents and an
append-only version chain, moving a head pointer. Run against both backends via a
fixture so the shared SQL logic and the in-memory store stay in lockstep."""

from __future__ import annotations

import pytest

from lessonforge.config import DocumentStoreConfig
from lessonforge.domain.document import (
    DocumentKind,
    DocumentVersion,
    StoredDocument,
    VersionOrigin,
)
from lessonforge.services.documents.store import (
    MemoryDocumentStore,
    SqliteDocumentStore,
)


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path):
    if request.param == "memory":
        return MemoryDocumentStore()
    return SqliteDocumentStore(tmp_path / "docs.db")


def _doc(**kw) -> StoredDocument:
    return StoredDocument(**{"title": "Photosynthesis", "grade": 10, "subject": "Science", **kw})


def _version(document_id: str, snapshot: dict, **kw) -> DocumentVersion:
    return DocumentVersion(document_id=document_id, snapshot=snapshot, **kw)


def test_create_get_list_roundtrip(store):
    doc = store.create(_doc(owner_id="teacher-1"))
    got = store.get(doc.id)
    assert got is not None and got.title == "Photosynthesis"
    assert got.kind is DocumentKind.lesson
    assert got.grade == 10 and got.subject == "Science"
    assert [d.id for d in store.list("teacher-1")] == [doc.id]
    assert store.list("someone-else") == []


def test_missing_document_returns_none(store):
    assert store.get("nope") is None


def test_add_version_points_head_and_orders(store):
    doc = store.create(_doc())
    v1 = store.add_version(_version(doc.id, {"topic": "A"}))
    v2 = store.add_version(_version(doc.id, {"topic": "B"}, parent_id=v1.id,
                                   origin=VersionOrigin.refine))
    assert store.get(doc.id).head_version_id == v2.id  # head follows latest
    assert [v.id for v in store.versions(doc.id)] == [v1.id, v2.id]  # oldest first
    assert store.get_version(v1.id).snapshot == {"topic": "A"}


def test_version_carries_audit_fields(store):
    doc = store.create(_doc())
    v = store.add_version(_version(
        doc.id, {"topic": "A"}, origin=VersionOrigin.refine,
        instruction="use the river", diff={"engagement_hook": {"before": 1, "after": 2}},
        grounding_sources=["Book, Unit 2"], scores={"engagement": 0.9},
    ))
    got = store.get_version(v.id)
    assert got.origin is VersionOrigin.refine
    assert got.instruction == "use the river"
    assert got.diff == {"engagement_hook": {"before": 1, "after": 2}}
    assert got.grounding_sources == ["Book, Unit 2"]
    assert got.scores == {"engagement": 0.9}


def test_set_head_moves_the_pointer(store):
    doc = store.create(_doc())
    v1 = store.add_version(_version(doc.id, {"topic": "A"}))
    store.add_version(_version(doc.id, {"topic": "B"}, parent_id=v1.id))
    moved = store.set_head(doc.id, v1.id)
    assert moved is not None and moved.head_version_id == v1.id


def test_set_head_rejects_foreign_version(store):
    a = store.create(_doc())
    b = store.create(_doc())
    vb = store.add_version(_version(b.id, {"topic": "B"}))
    assert store.set_head(a.id, vb.id) is None  # version belongs to another document


def test_add_version_to_unknown_document_raises(store):
    with pytest.raises(KeyError):
        store.add_version(_version("ghost", {"topic": "X"}))


def test_delete_removes_document_and_versions(store):
    doc = store.create(_doc())
    v = store.add_version(_version(doc.id, {"topic": "A"}))
    assert store.delete(doc.id) is True
    assert store.get(doc.id) is None
    assert store.get_version(v.id) is None
    assert store.versions(doc.id) == []
    assert store.delete(doc.id) is False  # already gone


def test_sqlite_persists_across_reopen(tmp_path):
    path = tmp_path / "docs.db"
    s1 = SqliteDocumentStore(path)
    doc = s1.create(_doc(owner_id="t"))
    s1.add_version(_version(doc.id, {"topic": "A"}))
    # a fresh instance on the same file sees the data
    s2 = SqliteDocumentStore(path)
    assert [d.id for d in s2.list("t")] == [doc.id]
    assert s2.get(doc.id).head_version_id is not None


def test_from_config_builds_the_right_backend(tmp_path):
    mem = MemoryDocumentStore.from_config(DocumentStoreConfig(provider="memory"))
    assert isinstance(mem, MemoryDocumentStore)
    sql = SqliteDocumentStore.from_config(
        DocumentStoreConfig(provider="sqlite", path=str(tmp_path / "d.db"))
    )
    assert isinstance(sql, SqliteDocumentStore)
