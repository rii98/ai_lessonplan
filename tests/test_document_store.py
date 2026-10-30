"""DocumentStore contract — every test runs on the memory AND the Postgres store."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from lessonforge.config import DocumentStoreConfig
from lessonforge.domain.document import (
    DocumentKind,
    DocumentVersion,
    StoredDocument,
    VersionOrigin,
    Visibility,
)
from lessonforge.pagination import Keyset
from lessonforge.services.documents.store import MemoryDocumentStore, PostgresDocumentStore
from lessonforge.services.registry import build_document_store
from tests.conftest import make_principal, pg_principal


@pytest.fixture(params=["memory", "postgres"])
def env(request):
    """(store, make_owner(tenant, name) -> principal) per backend."""
    if request.param == "memory":
        store = MemoryDocumentStore()

        def owner(tenant="sch-1", name=None):
            p = make_principal(tenant=tenant, name=name)
            store.owner_names[p.id] = p.display_name
            return p
        return store, owner
    db = request.getfixturevalue("pg")
    return PostgresDocumentStore(db), lambda tenant="sch-1", name=None: pg_principal(
        db, tenant=tenant, name=name)


def _new(store, owner, *, title="Plants", tenant="sch-1", snapshot=None, **kw):
    doc = StoredDocument(owner_id=owner.id, tenant_id=tenant, title=title, grade=6,
                         subject="Science", **kw)
    v = DocumentVersion(document_id=doc.id, snapshot=snapshot or {"v": 1})
    return store.create_with_version(doc, v)


def test_create_with_version_sets_head_and_reads_in_one_go(env):
    store, owner = env
    p = owner()
    doc, v = _new(store, p)
    assert doc.head_version_id == v.id
    got_doc, head = store.head(doc.id)
    assert got_doc.title == "Plants" and head.snapshot == {"v": 1}
    assert store.get("nope") is None and store.head("nope") is None


def test_add_version_is_owner_guarded_and_ordered(env):
    store, owner = env
    a, b = owner(), owner()
    doc, v1 = _new(store, a)
    v2 = store.add_version(DocumentVersion(document_id=doc.id, parent_id=v1.id,
                                           origin=VersionOrigin.refine, snapshot={"v": 2},
                                           instruction="tighten", diff={"a": 1},
                                           grounding_sources=["S"], scores={"engagement": 0.9}),
                           owner_id=a.id)
    with pytest.raises(KeyError):
        store.add_version(DocumentVersion(document_id=doc.id, snapshot={}), owner_id=b.id)
    hist = store.versions(doc.id)
    assert [h.id for h in hist] == [v1.id, v2.id]
    assert hist[1].instruction == "tighten" and hist[1].scores == {"engagement": 0.9}
    assert not hasattr(hist[1], "snapshot")                       # history is snapshot-free
    assert store.head(doc.id)[1].id == v2.id
    assert store.get_version(doc.id, v2.id).snapshot == {"v": 2}


def test_set_head_owner_and_document_guarded(env):
    store, owner = env
    a, b = owner(), owner()
    doc, v1 = _new(store, a)
    _other, ov = _new(store, a, title="other")
    store.add_version(DocumentVersion(document_id=doc.id, parent_id=v1.id, snapshot={}),
                      owner_id=a.id)
    assert store.set_head(doc.id, v1.id, owner_id=b.id) is None      # not the owner
    assert store.set_head(doc.id, ov.id, owner_id=a.id) is None      # foreign version
    assert store.set_head(doc.id, v1.id, owner_id=a.id).head_version_id == v1.id


def test_latest_child_picks_newest_branch(env):
    store, owner = env
    a = owner()
    doc, root = _new(store, a)
    t = datetime.now(UTC)
    store.add_version(DocumentVersion(document_id=doc.id, parent_id=root.id, snapshot={},
                                      created_at=t), owner_id=a.id)
    newer = store.add_version(DocumentVersion(document_id=doc.id, parent_id=root.id, snapshot={},
                                              created_at=t + timedelta(seconds=1)), owner_id=a.id)
    assert store.latest_child(doc.id, root.id).id == newer.id
    assert store.latest_child(doc.id, newer.id) is None


def test_list_owned_keyset_and_kind_filter(env):
    store, owner = env
    a, b = owner(), owner()
    base = datetime(2026, 1, 1, tzinfo=UTC)
    for i in range(3):
        _new(store, a, title=f"L{i}", created_at=base, updated_at=base + timedelta(minutes=i))
    _new(store, a, title="U", kind=DocumentKind.unit, created_at=base,
         updated_at=base + timedelta(minutes=9))
    _new(store, b, title="theirs")
    p1 = store.list_owned(a.id, limit=2)
    assert [d.title for d in p1.items] == ["U", "L2"]
    p2 = store.list_owned(a.id, limit=2, cursor=Keyset.decode(p1.next_cursor))
    assert [d.title for d in p2.items] == ["L1", "L0"] and p2.next_cursor is None
    assert [d.title for d in store.list_owned(a.id, kind=DocumentKind.unit, limit=10).items] == ["U"]


def test_sharing_lists_only_same_school_with_owner_names(env):
    store, owner = env
    sita = owner(name="Sita Sharma")
    doc, _ = _new(store, sita, title="Photosynthesis")
    _new(store, sita, title="Private draft")
    far = owner(tenant="sch-9")
    far_doc, _ = _new(store, far, tenant="sch-9", title="Elsewhere")
    store.set_visibility(far_doc.id, Visibility.school, owner_id=far.id)

    assert store.list_shared("sch-1", limit=10).items == []
    shared = store.set_visibility(doc.id, Visibility.school, owner_id=sita.id)
    assert shared.visibility == Visibility.school and shared.shared_at is not None
    items = store.list_shared("sch-1", limit=10).items
    assert [d.title for d in items] == ["Photosynthesis"] and items[0].owner_name == "Sita Sharma"
    assert [d.title for d in store.list_shared("sch-1", q="photo", limit=10).items] == ["Photosynthesis"]
    assert store.list_shared("sch-1", q="%", limit=10).items == []        # LIKE wildcards escaped
    assert store.list_shared("sch-1", grade=7, limit=10).items == []
    store.set_visibility(doc.id, Visibility.private, owner_id=sita.id)
    assert store.list_shared("sch-1", limit=10).items == []               # un-share is immediate


def test_visibility_is_owner_only_and_needs_a_school(env):
    store, owner = env
    a, b = owner(), owner()
    doc, _ = _new(store, a)
    assert store.set_visibility(doc.id, Visibility.school, owner_id=b.id) is None
    homeless, _ = _new(store, a, tenant=None)
    with pytest.raises(ValueError):
        store.set_visibility(homeless.id, Visibility.school, owner_id=a.id)


def test_delete_is_owner_only_cascades_versions_and_keeps_copies(env):
    store, owner = env
    a, b = owner(), owner()
    doc, v = _new(store, a)
    copy, _ = _new(store, b, copied_from=doc.id)
    assert store.delete(doc.id, owner_id=b.id) is False
    assert store.delete(doc.id, owner_id=a.id) is True
    assert store.get(doc.id) is None and store.get_version(doc.id, v.id) is None
    survivor = store.get(copy.id)
    assert survivor is not None and survivor.copied_from is None   # ON DELETE SET NULL


def test_from_config_builds_backends():
    assert isinstance(build_document_store(DocumentStoreConfig(provider="memory")),
                      MemoryDocumentStore)
    with pytest.raises(ValueError):
        build_document_store(DocumentStoreConfig(provider="sqlite"))
