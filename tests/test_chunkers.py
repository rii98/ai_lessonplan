"""Chunkers split documents deterministically; content-hash ids make ingestion
idempotent."""

from __future__ import annotations

import pytest

from lessonforge.rag.chunkers import ParagraphChunker
from lessonforge.rag.documents import Collection


def test_short_text_stays_one_chunk():
    chunks = ParagraphChunker().chunk(
        "A single short paragraph.", collection=Collection.pedagogical, source="s"
    )
    assert len(chunks) == 1
    assert chunks[0].collection is Collection.pedagogical
    assert chunks[0].source == "s"


def test_paragraphs_pack_up_to_budget():
    text = "\n\n".join([f"Paragraph number {i} with some words." for i in range(10)])
    chunks = ParagraphChunker(max_chars=80).split(text)
    assert len(chunks) > 1
    assert all(len(c) <= 80 or "\n\n" not in c for c in chunks)  # only single paras may exceed


def test_chunking_is_deterministic_and_ids_are_stable():
    text = "Para one.\n\nPara two is here.\n\nPara three closes it out."
    a = ParagraphChunker(max_chars=30).chunk(text, collection=Collection.exemplar, source="s")
    b = ParagraphChunker(max_chars=30).chunk(text, collection=Collection.exemplar, source="s")
    assert [c.id for c in a] == [c.id for c in b]
    assert [c.text for c in a] == [c.text for c in b]


def test_metadata_flows_into_payload_but_collection_is_first_class():
    (chunk,) = ParagraphChunker().chunk(
        "text", collection=Collection.curriculum, source="s",
        metadata={"grade": 6, "subject": "Science", "collection": "ignored"},
    )
    payload = chunk.payload()
    assert payload["grade"] == 6
    assert payload["subject"] == "Science"
    assert payload["collection"] == "curriculum"  # from the enum, not the metadata noise
    assert payload["text"] == "text"
    assert payload["source"] == "s"


def test_same_text_different_collection_gets_different_id():
    a = ParagraphChunker().chunk("x y z", collection=Collection.curriculum, source="s")[0]
    b = ParagraphChunker().chunk("x y z", collection=Collection.pedagogical, source="s")[0]
    assert a.id != b.id


def test_blank_text_yields_no_chunks():
    assert ParagraphChunker().chunk("\n\n   \n\n", collection=Collection.exemplar, source="s") == []


def test_invalid_max_chars_rejected():
    with pytest.raises(ValueError, match="max_chars"):
        ParagraphChunker(max_chars=0)
