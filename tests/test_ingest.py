"""Ingestor: load → chunk → embed → upsert, idempotently, through interfaces
only (fakes). Also exercises the seed corpus and the CLI entry point."""

from __future__ import annotations

import pytest

from lessonforge.rag.documents import Collection, Document
from lessonforge.rag.ingest import Ingestor, default_seed_dir, ingest_seed, main


def _ingestor(fake_embedder, fake_store) -> Ingestor:
    return Ingestor(embedder=fake_embedder, vector_store=fake_store)


def test_ingest_documents_embeds_and_upserts(fake_embedder, fake_store):
    ing = _ingestor(fake_embedder, fake_store)
    docs = [
        Document(id="d1", text="biotic and abiotic components", source="s1",
                 metadata={"grade": 6, "subject": "Science"}),
        Document(id="d2", text="producers consumers decomposers", source="s2",
                 metadata={"grade": 6, "subject": "Science"}),
    ]
    report = ing.ingest_documents(Collection.curriculum, docs)
    assert report.documents == 2
    assert report.chunks == 2
    assert report.sources == {"s1", "s2"}
    recs = fake_store.data["curriculum"]
    assert len(recs) == 2
    assert recs[0].payload["grade"] == 6
    assert recs[0].payload["source"] == "s1"
    assert len(recs[0].vector) == fake_embedder.dim


def test_ingest_is_idempotent(fake_embedder, fake_store):
    ing = _ingestor(fake_embedder, fake_store)
    docs = [Document(id="d1", text="stable content", source="s", metadata={"grade": 6})]
    ing.ingest_documents(Collection.pedagogical, docs)
    ids_first = [r.id for r in fake_store.data["pedagogical"]]
    # re-ingest the same content → same content-hash ids (a real store upserts,
    # not duplicates). The fake appends, so we assert the ids match, not the count.
    ing.ingest_documents(Collection.pedagogical, docs)
    ids_second = [r.id for r in fake_store.data["pedagogical"]]
    assert ids_first[0] == ids_second[0]


def test_per_document_collection_override(fake_embedder, fake_store):
    ing = _ingestor(fake_embedder, fake_store)
    docs = [Document(id="d1", text="a local goat", source="s",
                     metadata={"collection": "local_context"})]
    # target says curriculum, but the record overrides to local_context
    ing.ingest_documents(Collection.curriculum, docs)
    assert fake_store.data["local_context"]
    assert "curriculum" not in fake_store.data or not fake_store.data["curriculum"]


def test_seed_corpus_ingests_all_four_collections(fake_embedder, fake_store):
    assert default_seed_dir().exists(), "seed corpus missing"
    ing = _ingestor(fake_embedder, fake_store)
    report = ingest_seed(ing)
    assert report.chunks > 0
    for c in Collection:
        assert fake_store.data.get(c.value), f"{c.value} not populated"
    # provenance labels are honest about being seed/authored
    all_sources = {r.payload["source"] for recs in fake_store.data.values() for r in recs}
    assert any("Seed" in s or "Exemplar" in s for s in all_sources)


def test_cli_source_ingest(tmp_path, monkeypatch, fake_embedder, fake_store, capsys):
    p = tmp_path / "peda.jsonl"
    p.write_text('{"text": "a misconception about clouds", "grade": 6}\n', encoding="utf-8")
    monkeypatch.setattr(
        "lessonforge.rag.ingest._build_default_ingestor",
        lambda: _ingestor(fake_embedder, fake_store),
    )
    rc = main(["--source", str(p), "--collection", "pedagogical", "--subject", "Science"])
    assert rc == 0
    assert fake_store.data["pedagogical"]
    assert fake_store.data["pedagogical"][0].payload["subject"] == "Science"
    assert "ingested" in capsys.readouterr().out


def test_cli_seed(monkeypatch, fake_embedder, fake_store, capsys):
    monkeypatch.setattr(
        "lessonforge.rag.ingest._build_default_ingestor",
        lambda: _ingestor(fake_embedder, fake_store),
    )
    rc = main(["--seed"])
    assert rc == 0
    assert "seed ingested" in capsys.readouterr().out
    assert fake_store.data["curriculum"]


def test_cli_markdown_source_with_metadata(
    tmp_path, monkeypatch, fake_embedder, fake_store
):
    p = tmp_path / "lesson.md"
    p.write_text("# Env\n\nBiotic and abiotic components.", encoding="utf-8")
    monkeypatch.setattr(
        "lessonforge.rag.ingest._build_default_ingestor",
        lambda: _ingestor(fake_embedder, fake_store),
    )
    rc = main(["--source", str(p), "--collection", "exemplar",
               "--format", "markdown", "--grade", "6", "--subject", "Science"])
    assert rc == 0
    rec = fake_store.data["exemplar"][0]
    assert rec.payload["grade"] == 6 and rec.payload["subject"] == "Science"


def test_cli_requires_something(monkeypatch, fake_embedder, fake_store):
    monkeypatch.setattr(
        "lessonforge.rag.ingest._build_default_ingestor",
        lambda: _ingestor(fake_embedder, fake_store),
    )
    with pytest.raises(SystemExit):
        main([])


def test_cli_source_without_collection_errors(
    tmp_path, monkeypatch, fake_embedder, fake_store
):
    p = tmp_path / "x.jsonl"
    p.write_text('{"text": "t"}\n', encoding="utf-8")
    monkeypatch.setattr(
        "lessonforge.rag.ingest._build_default_ingestor",
        lambda: _ingestor(fake_embedder, fake_store),
    )
    with pytest.raises(SystemExit):
        main(["--source", str(p)])
