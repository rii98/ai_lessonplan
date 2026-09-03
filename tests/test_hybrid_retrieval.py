"""Hybrid retrieval: dense ⊕ sparse when wired + supported, dense-only otherwise.
The Retriever/Ingestor never name a concrete store — they branch on the interface
(`supports_hybrid`) and degrade cleanly."""

from __future__ import annotations

from lessonforge.providers.base import SparseVector
from lessonforge.rag.documents import Collection, Document
from lessonforge.rag.ingest import Ingestor
from lessonforge.rag.retriever import Retriever
from tests.conftest import FakeEmbedder, FakeReranker, FakeSparseEmbedder, FakeVectorStore


class HybridStore(FakeVectorStore):
    """A store that advertises hybrid support and records how it was queried."""

    def __init__(self) -> None:
        super().__init__()
        self.hybrid_calls = 0
        self.dense_calls = 0
        self.sparse_seen: list[SparseVector] = []
        self.ensure_sparse: list[bool] = []

    @property
    def supports_hybrid(self) -> bool:
        return True

    def ensure_collection(self, name, dim, *, sparse=False):
        self.ensure_sparse.append(sparse)
        super().ensure_collection(name, dim, sparse=sparse)

    def search(self, name, vector, top_k, where=None):
        self.dense_calls += 1
        return super().search(name, vector, top_k, where=where)

    def hybrid_search(self, name, dense_vector, sparse_vector, top_k, where=None):
        self.hybrid_calls += 1
        self.sparse_seen.append(sparse_vector)
        return super().search(name, dense_vector, top_k, where=where)


def _seed(store, embedder):
    Ingestor(embedder=embedder, vector_store=store).ingest_documents(
        Collection.reference,
        [Document(id="r1", text="sound is produced by vibration", source="Book",
                  metadata={"grade": 7})],
    )


def test_sparse_embedder_is_deterministic_and_parallel():
    a = FakeSparseEmbedder().embed_sparse_one("sound and vibration")
    b = FakeSparseEmbedder().embed_sparse_one("sound and vibration")
    assert a.indices == b.indices and a.values == b.values
    assert len(a.indices) == len(a.values)


def test_retriever_uses_hybrid_when_wired_and_supported():
    store, embedder = HybridStore(), FakeEmbedder()
    _seed(store, embedder)
    r = Retriever(embedder=embedder, vector_store=store, reranker=FakeReranker(),
                  sparse_embedder=FakeSparseEmbedder(), hybrid=True)
    assert r.hybrid_active
    hits = r.retrieve("reference", "sound vibration")
    assert hits and store.hybrid_calls == 1 and store.dense_calls == 0
    assert store.sparse_seen and store.sparse_seen[0].indices  # the query was sparse-embedded


def test_no_sparse_embedder_means_dense_only():
    store, embedder = HybridStore(), FakeEmbedder()
    _seed(store, embedder)
    r = Retriever(embedder=embedder, vector_store=store, reranker=FakeReranker(), hybrid=True)
    assert not r.hybrid_active
    r.retrieve("reference", "sound")
    assert store.hybrid_calls == 0 and store.dense_calls == 1


def test_unsupported_store_degrades_to_dense_and_still_returns_hits():
    # a plain FakeVectorStore reports supports_hybrid=False → dense path
    store, embedder = FakeVectorStore(), FakeEmbedder()
    _seed(store, embedder)
    r = Retriever(embedder=embedder, vector_store=store, reranker=FakeReranker(),
                  sparse_embedder=FakeSparseEmbedder(), hybrid=True)
    assert not r.hybrid_active
    assert r.retrieve("reference", "sound vibration")  # base hybrid_search default → dense


def test_ingestor_provisions_sparse_and_stores_sparse_vectors_when_hybrid():
    store, embedder = HybridStore(), FakeEmbedder()
    Ingestor(embedder=embedder, vector_store=store,
             sparse_embedder=FakeSparseEmbedder(), hybrid=True).ingest_documents(
        Collection.reference,
        [Document(id="r1", text="sound is vibration", source="Book", metadata={})],
    )
    assert store.ensure_sparse == [True]
    assert all(rec.sparse_vector is not None for rec in store.data["reference"])
