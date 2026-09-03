"""Coverage-aware selection over the reranked union: soft per-collection floors,
priority weights, and the recycle/priority tie-break rules.

Exercises ``ChatRetriever._select`` directly with hand-built reranked chunks so
the allocation logic is tested independent of embedding/reranking."""

from __future__ import annotations

import pytest

from lessonforge.config import ChatRetrievalConfig
from lessonforge.services.chat.retrieve import ChatRetriever, ScoredChunk


def _cr(**kw):
    cfg = ChatRetrievalConfig(
        collections=["reference", "curriculum", "pedagogical", "exemplar", "local_context"],
        rerank_top_n=kw.pop("rerank_top_n", 6),
        **kw,
    )
    return ChatRetriever(retriever=None, config=cfg)  # _select needs no retriever


def _chunk(col, score, tag=""):
    return ScoredChunk(text=f"{col}-{tag or score}", source=f"{col}-src",
                       collection=col, score=score)


def _cols(selected):
    return [c.collection for c in selected]


def test_no_quota_no_weights_is_pure_relevance():
    cr = _cr(rerank_top_n=3)
    reranked = [_chunk("curriculum", 0.9), _chunk("curriculum", 0.8),
                _chunk("pedagogical", 0.7), _chunk("reference", 0.1)]
    out = cr._select(reranked)
    assert [c.score for c in out] == [0.9, 0.8, 0.7]  # top-3 by score, collection-blind


def test_min_per_collection_reserves_a_slot_each():
    cr = _cr(rerank_top_n=4, min_per_collection=1)
    # curriculum would otherwise sweep all 4 slots
    reranked = [_chunk("curriculum", 0.99, "a"), _chunk("curriculum", 0.98, "b"),
                _chunk("curriculum", 0.97, "c"), _chunk("curriculum", 0.96, "d"),
                _chunk("pedagogical", 0.20), _chunk("reference", 0.10)]
    out = cr._select(reranked)
    cols = set(_cols(out))
    assert {"pedagogical", "reference"} <= cols  # each reserved a slot
    assert len(out) == 4


def test_override_gives_reference_more_slots():
    cr = _cr(rerank_top_n=4, min_per_collection=1,
             min_per_collection_overrides={"reference": 2})
    reranked = [_chunk("curriculum", 0.99, "a"), _chunk("curriculum", 0.98, "b"),
                _chunk("reference", 0.30, "r1"), _chunk("reference", 0.20, "r2"),
                _chunk("pedagogical", 0.10)]
    out = cr._select(reranked)
    assert _cols(out).count("reference") == 2


def test_weights_reorder_final_citations():
    # equal raw scores; reference weight should lift it to the front
    cr = _cr(rerank_top_n=3, collection_weights={"reference": 1.5})
    reranked = [_chunk("curriculum", 0.5), _chunk("pedagogical", 0.5),
                _chunk("reference", 0.5)]
    out = cr._select(reranked)
    assert out[0].collection == "reference"


def test_soft_floor_not_padded_when_collection_absent():
    # only two collections present; floors for the rest simply go unfilled and
    # their slots are recycled to the best available chunks.
    cr = _cr(rerank_top_n=4, min_per_collection=1)
    reranked = [_chunk("curriculum", 0.9, "a"), _chunk("curriculum", 0.8, "b"),
                _chunk("curriculum", 0.7, "c"), _chunk("reference", 0.4)]
    out = cr._select(reranked)
    assert len(out) == 4  # no wasted slots
    assert set(_cols(out)) == {"curriculum", "reference"}


def test_min_score_blocks_quota_filling_with_weak_hits():
    cr = _cr(rerank_top_n=3, min_per_collection=1, min_score=0.25)
    reranked = [_chunk("curriculum", 0.9, "a"), _chunk("curriculum", 0.8, "b"),
                _chunk("pedagogical", 0.05)]  # below floor → not reserved
    out = cr._select(reranked)
    assert "pedagogical" not in _cols(out)
    assert _cols(out) == ["curriculum", "curriculum"]  # weak hit excluded entirely


def test_floors_capped_at_top_n_by_priority():
    # three collections each want 2 (=6) but only 3 slots: priority (weight, then
    # config order) decides who gets them. reference is weighted highest.
    cr = _cr(rerank_top_n=3, min_per_collection=2,
             collection_weights={"reference": 2.0})
    reranked = [_chunk("reference", 0.5, "r1"), _chunk("reference", 0.4, "r2"),
                _chunk("curriculum", 0.9, "c1"), _chunk("curriculum", 0.8, "c2"),
                _chunk("pedagogical", 0.7, "p1"), _chunk("pedagogical", 0.6, "p2")]
    out = cr._select(reranked)
    assert len(out) == 3
    assert _cols(out).count("reference") == 2  # highest priority filled its floor first


@pytest.mark.parametrize("n", [1, 2, 5, 10])
def test_never_exceeds_top_n(n):
    cr = _cr(rerank_top_n=n, min_per_collection=1,
             min_per_collection_overrides={"reference": 3})
    reranked = [_chunk(c, 0.5, str(i)) for i, c in enumerate(
        ["reference", "reference", "curriculum", "pedagogical", "exemplar", "local_context"])]
    assert len(cr._select(reranked)) <= n
