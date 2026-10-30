"""Live contract: knowledge-space isolation enforced by the REAL Qdrant adapter
on every read and delete path (§16). Skipped unless RUN_INTEGRATION=1 and Qdrant
is reachable. Uses a fake embedder — the adapter, not the model, is under test."""

from __future__ import annotations

import os
import uuid

import pytest

from lessonforge.config import VectorStoreConfig
from lessonforge.knowledge.scope import AccessScope
from lessonforge.providers.base import SparseVector, VectorRecord
from lessonforge.providers.vectorstore.qdrant import QdrantVectorStore, _point_id
from tests.conftest import FakeEmbedder

pytestmark = pytest.mark.integration

A, B, G = AccessScope.of("global", "school:a"), AccessScope.of("global", "school:b"), AccessScope.public()


@pytest.fixture
def store():
    if os.environ.get("RUN_INTEGRATION") != "1":
        pytest.skip("integration disabled")
    s = QdrantVectorStore.from_config(VectorStoreConfig(
        provider="qdrant", url=os.environ.get("QDRANT_URL", "http://localhost:6333"),
        collection_prefix=f"lfspace_{uuid.uuid4().hex[:8]}"))
    if not s.health():
        pytest.skip("Qdrant not reachable")
    yield s
    client = s._c()
    for c in client.get_collections().collections:
        if c.name.startswith(s.prefix):
            client.delete_collection(c.name)


def _rec(emb, rid, text, space, doc="d1"):
    return VectorRecord(id=rid, vector=emb.embed_one(text),
                        payload={"text": text, "_space": space, "doc_id": doc, "grade": 6},
                        sparse_vector=SparseVector(indices=[1, 2], values=[1.0, 1.0]))


@pytest.mark.parametrize("hybrid", [False, True])
def test_every_read_and_delete_is_space_scoped(store, hybrid):
    emb = FakeEmbedder()
    store.ensure_collection("reference", emb.dim, sparse=hybrid)
    store.upsert("reference", [_rec(emb, "g1", "shared light", "global", "gdoc"),
                               _rec(emb, "a1", "school a secret", "school:a", "adoc"),
                               _rec(emb, "b1", "school b notes", "school:b", "bdoc")])
    with pytest.raises(ValueError, match="knowledge space"):
        store.upsert("reference", [VectorRecord(id="x", vector=emb.embed_one("x"), payload={})])

    v = emb.embed_one("anything")
    texts = lambda hits: {h.payload["text"] for h in hits}
    assert texts(store.search("reference", v, 10, scope=B)) == {"shared light", "school b notes"}
    assert texts(store.search("reference", v, 10, where={"grade": 6}, scope=A)) == {
        "shared light", "school a secret"}
    if hybrid:
        sp = SparseVector(indices=[1, 2], values=[1.0, 1.0])
        assert "school a secret" not in texts(store.hybrid_search("reference", v, sp, 10, scope=B))
    assert store.fetch("reference", {"doc_id": "adoc"}, scope=B) == []
    assert len(store.fetch("reference", {"doc_id": "adoc"}, scope=A)) == 1
    page, _ = store.scroll("reference", limit=10, scope=G)
    assert texts(page) == {"shared light"}
    assert (store.count("reference", scope=B), store.count("reference", scope=AccessScope.all())) == (2, 3)
    assert store.search("reference", v, 10, scope=AccessScope(frozenset())) == []   # empty = nothing

    store.delete("reference", ["a1"], scope=B)            # B cannot delete A's point
    assert store.count("reference", scope=A) == 2
    store.delete_where("reference", {"doc_id": "adoc"}, scope=AccessScope.of("school:b"))
    assert store.count("reference", scope=A) == 2
    store.delete_where("reference", {"doc_id": "adoc"}, scope=AccessScope.of("school:a"))
    assert store.count("reference", scope=A) == 1


def test_tenant_index_and_backfill_of_legacy_points(store):
    from qdrant_client.models import PointStruct

    emb = FakeEmbedder()
    store.ensure_collection("pedagogical", emb.dim)
    full = store._name("pedagogical")
    info = store._c().get_collection(full)
    assert "_space" in info.payload_schema and "doc_id" in info.payload_schema
    # a point written before spaces existed (bypassing the adapter's guard)
    store._c().upsert(full, points=[PointStruct(id=_point_id("old"), vector=emb.embed_one("old"),
                                                payload={"text": "old tip", "_source_id": "old"})])
    assert store.count("pedagogical", scope=G) == 0       # invisible until it has a space
    assert store.backfill_space("pedagogical", "global") == 1
    assert store.backfill_space("pedagogical", "global") == 0              # idempotent
    assert store.count("pedagogical", scope=G) == 1
