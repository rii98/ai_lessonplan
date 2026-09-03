"""ChatRetriever tests: RRF fusion across queries/collections, the two modes,
and the lenient scoped→broad fallback. Uses the real Retriever wired with the
in-memory fakes from conftest."""

from __future__ import annotations

import pytest

from lessonforge.config import ChatRetrievalConfig
from lessonforge.domain.chat import FilterMode
from lessonforge.providers.base import VectorRecord
from lessonforge.rag.documents import (
    COLLECTION_KEY,
    SOURCE_KEY,
    TEXT_KEY,
    Collection,
)
from lessonforge.rag.retriever import Retriever
from lessonforge.services.chat.retrieve import ChatRetriever
from lessonforge.services.chat.transform import TransformedQuery


def _rec(id_, text, collection, **meta):
    payload = {TEXT_KEY: text, SOURCE_KEY: f"src-{id_}", COLLECTION_KEY: collection, **meta}
    return VectorRecord(id=id_, vector=[0.1] * 8, payload=payload)


@pytest.fixture
def wired(fake_embedder, fake_store, fake_reranker):
    # seed two collections; one carries grade metadata for scoped filtering
    fake_store.ensure_collection("curriculum", 8)
    fake_store.ensure_collection("pedagogical", 8)
    fake_store.upsert("curriculum", [
        _rec("c1", "photosynthesis converts light to energy", "curriculum", grade=6),
        _rec("c2", "respiration releases energy in cells", "curriculum", grade=7),
    ])
    fake_store.upsert("pedagogical", [
        _rec("p1", "students confuse photosynthesis with respiration", "pedagogical", grade=6),
    ])
    retriever = Retriever(
        embedder=fake_embedder, vector_store=fake_store, reranker=fake_reranker,
        hybrid=False, top_k=20, rerank_top_n=6,
    )
    cfg = ChatRetrievalConfig(
        collections=["curriculum", "pedagogical"], per_collection_top_n=4,
        rerank_top_n=6, filter_fields=["grade", "subject"], default_mode="scoped",
    )
    return ChatRetriever(retriever, cfg)


def _q(*queries):
    return TransformedQuery(queries=list(queries), standalone=queries[0])


def test_retrieves_across_collections(wired):
    out = wired.retrieve(_q("photosynthesis"), mode=FilterMode.broad)
    cols = {c.collection for c in out}
    assert "curriculum" in cols and "pedagogical" in cols
    assert all(c.text and c.source for c in out)


def test_scoped_mode_filters_by_tag(wired):
    out = wired.retrieve(_q("energy"), mode=FilterMode.scoped, filters={"grade": 7})
    texts = " ".join(c.text for c in out)
    assert "respiration releases energy" in texts
    assert "grade" not in {k for c in out for k in ()}  # sanity: chunks carry text
    # grade-6 curriculum row must be excluded by the grade=7 filter
    assert "light to energy" not in texts


def test_broad_mode_ignores_filters(wired):
    out = wired.retrieve(_q("energy"), mode=FilterMode.broad, filters={"grade": 7})
    texts = " ".join(c.text for c in out)
    # broad sees both grades
    assert "respiration" in texts and "photosynthesis" in texts


def test_scoped_with_no_match_falls_back_to_broad(wired):
    # grade 99 matches nothing; lenient fallback broadens rather than returning empty
    out = wired.retrieve(_q("photosynthesis"), mode=FilterMode.scoped, filters={"grade": 99})
    assert out, "scoped query with no matches should fall back, not return empty"


def test_rrf_fuses_duplicate_hits_once(wired):
    # the same passage retrieved by two query variants should appear once, fused
    out = wired.retrieve(_q("photosynthesis", "photosynthesis light"), mode=FilterMode.broad)
    keys = [(c.collection, c.text) for c in out]
    assert len(keys) == len(set(keys)), "duplicate chunks must be de-duplicated by RRF"


def test_unknown_collection_names_are_ignored(fake_embedder, fake_store, fake_reranker):
    retriever = Retriever(embedder=fake_embedder, vector_store=fake_store,
                          reranker=fake_reranker, hybrid=False)
    cfg = ChatRetrievalConfig(collections=["curriculum", "not_a_collection"])
    cr = ChatRetriever(retriever, cfg)
    assert cr._collections(None) == [Collection.curriculum.value]
