"""Loaders turn raw sources into Documents. Registry resolves format → loader;
unknown formats fail loudly (same contract as the provider registry)."""

from __future__ import annotations

import pytest

from lessonforge.rag.loaders import JsonlLoader, build_loader, register_loader


def test_jsonl_loader_reads_records_and_metadata(tmp_path):
    p = tmp_path / "c.jsonl"
    p.write_text(
        '{"id": "a1", "text": "biotic vs abiotic", "source": "src", '
        '"grade": 6, "subject": "Science", "standard": "X1"}\n'
        '// a comment line is skipped\n'
        '\n'
        '{"text": "producers and consumers", "topic": "Env"}\n',
        encoding="utf-8",
    )
    docs = list(JsonlLoader().load(p))
    assert len(docs) == 2
    d0 = docs[0]
    assert d0.id == "a1"
    assert d0.text == "biotic vs abiotic"
    assert d0.source == "src"
    assert d0.metadata == {"grade": 6, "subject": "Science", "standard": "X1"}
    # second record: defaults for id/source, nested-free metadata
    assert docs[1].metadata == {"topic": "Env"}
    assert docs[1].source.endswith(":4")  # line number provenance fallback


def test_jsonl_nested_metadata_merges(tmp_path):
    p = tmp_path / "c.jsonl"
    p.write_text('{"text": "t", "grade": 6, "metadata": {"unit": "env", "grade": 7}}\n',
                 encoding="utf-8")
    (doc,) = list(JsonlLoader().load(p))
    # top-level grade wins over nested; nested extras retained
    assert doc.metadata["grade"] == 6
    assert doc.metadata["unit"] == "env"


def test_jsonl_missing_text_is_an_error(tmp_path):
    p = tmp_path / "bad.jsonl"
    p.write_text('{"source": "x"}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="missing non-empty 'text'"):
        list(JsonlLoader().load(p))


def test_jsonl_invalid_json_reports_line(tmp_path):
    p = tmp_path / "bad.jsonl"
    p.write_text('{"text": "ok"}\n{not json}\n', encoding="utf-8")
    with pytest.raises(ValueError, match=r":2: invalid JSON"):
        list(JsonlLoader().load(p))


def test_markdown_loader_reads_whole_file(tmp_path):
    p = tmp_path / "lesson.md"
    p.write_text("# Title\n\nBody paragraph.", encoding="utf-8")
    (doc,) = list(build_loader("markdown").load(p))
    assert doc.id == "lesson"
    assert "Body paragraph" in doc.text
    assert doc.source == "lesson.md"


def test_unknown_loader_fails_loudly():
    with pytest.raises(ValueError, match="Unknown loader format 'pdf'"):
        build_loader("pdf")


def test_duplicate_loader_registration_rejected():
    with pytest.raises(ValueError, match="already registered"):

        @register_loader("jsonl")
        class _Dupe:  # pragma: no cover
            pass
