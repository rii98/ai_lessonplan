"""Ingestor: load → chunk → embed → upsert into a vector-store collection.

Depends only on the :class:`Embedder` and :class:`VectorStore` interfaces plus a
:class:`Chunker`, so the whole pipeline runs in-process against fakes in tests
and against Qdrant + FastEmbed in production with the same code.

Ingestion is idempotent: chunk ids are content hashes, so re-running ``--seed``
upserts the same points rather than duplicating them.

CLI::

    python -m lessonforge.rag.ingest --seed
    python -m lessonforge.rag.ingest --source corpus/raw/foo.jsonl --collection pedagogical
    python -m lessonforge.rag.ingest --source lessonplan_reference/lp1.md \
        --collection exemplar --format markdown --grade 6 --subject Science
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..providers.base import Embedder, VectorRecord, VectorStore
from .chunkers import Chunker, ParagraphChunker
from .documents import Chunk, Collection, Document
from .loaders import build_loader, document_from_record


@dataclass(slots=True)
class IngestReport:
    """What an ingest run did — returned for programmatic use and printed by CLI."""

    collection: Collection
    documents: int = 0
    chunks: int = 0
    sources: set[str] = field(default_factory=set)

    def merge(self, other: IngestReport) -> None:
        self.documents += other.documents
        self.chunks += other.chunks
        self.sources |= other.sources


class Ingestor:
    def __init__(
        self,
        *,
        embedder: Embedder,
        vector_store: VectorStore,
        chunker: Chunker | None = None,
        batch_size: int = 64,
    ) -> None:
        self.embedder = embedder
        self.vector_store = vector_store
        self.chunker = chunker or ParagraphChunker()
        self.batch_size = batch_size

    # ── core ────────────────────────────────────────────────────────────────
    def ingest_documents(
        self, collection: Collection, docs: Iterable[Document]
    ) -> IngestReport:
        report = IngestReport(collection=collection)
        # group chunks by their actual target collection so a mixed file (records
        # that declare their own `collection`) lands in the right collections.
        by_collection: dict[Collection, list[Chunk]] = {}
        for doc in docs:
            report.documents += 1
            report.sources.add(doc.source)
            target = _coerce_collection(doc.metadata.get("collection"), collection)
            by_collection.setdefault(target, []).extend(
                self.chunker.chunk(
                    doc.text, collection=target, source=doc.source, metadata=doc.metadata
                )
            )

        for target, chunks in by_collection.items():
            if not chunks:
                continue
            self.vector_store.ensure_collection(target.value, self.embedder.dim)
            for batch in _batched(chunks, self.batch_size):
                vectors = self.embedder.embed([c.text for c in batch])
                records = [
                    VectorRecord(id=c.id, vector=v, payload=c.payload())
                    for c, v in zip(batch, vectors, strict=True)
                ]
                self.vector_store.upsert(target.value, records)
                report.chunks += len(records)
        return report

    def ingest_source(
        self,
        path: str | Path,
        collection: Collection,
        *,
        fmt: str = "jsonl",
        extra_metadata: dict[str, Any] | None = None,
    ) -> IngestReport:
        loader = build_loader(fmt)
        docs = list(loader.load(path))
        if extra_metadata:
            for d in docs:
                d.metadata = {**extra_metadata, **d.metadata}
        return self.ingest_documents(collection, docs)

    def ingest_records(
        self,
        collection: Collection,
        records: list[dict[str, Any]],
        *,
        source_label: str = "upload",
    ) -> IngestReport:
        """Ingest JSONL-style record dicts posted in-memory (the corpus UI path).

        Reuses :func:`document_from_record`, so records validate and pick up
        metadata exactly as if they had been read from a ``.jsonl`` file — and
        stay idempotent (re-submitting the same content updates in place).
        A record may set its own ``collection`` to override ``collection``."""
        docs = [
            document_from_record(
                r, source_default=f"{source_label}:{i}", id_default=f"{source_label}:{i}"
            )
            for i, r in enumerate(records, 1)
        ]
        return self.ingest_documents(collection, docs)


def _coerce_collection(value: Any, default: Collection) -> Collection:
    if value is None:
        return default
    try:
        return Collection(value)
    except ValueError:
        return default


def _batched(items: list[Any], n: int) -> Iterable[list[Any]]:
    for i in range(0, len(items), n):
        yield items[i : i + n]


# ── seed corpus + CLI ────────────────────────────────────────────────────────

def default_seed_dir() -> Path:
    """corpus/seed relative to the repo root (parents[3] of this file)."""
    return Path(__file__).resolve().parents[3] / "corpus" / "seed"


def ingest_seed(ingestor: Ingestor, seed_dir: Path | None = None) -> IngestReport:
    """Ingest ``corpus/seed/<collection>.jsonl`` for every collection that exists."""
    seed_dir = seed_dir or default_seed_dir()
    total = IngestReport(collection=Collection.curriculum)  # collection field is nominal here
    for collection in Collection:
        path = seed_dir / f"{collection.value}.jsonl"
        if path.exists():
            total.merge(ingestor.ingest_source(path, collection, fmt="jsonl"))
    return total


def _build_default_ingestor() -> Ingestor:
    # local import so the module imports cheaply (no provider construction) in tests
    from ..container import Container

    c = Container.from_settings()
    return Ingestor(embedder=c.embedder, vector_store=c.vector_store)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ingest a corpus into the vector store.")
    parser.add_argument("--seed", action="store_true", help="ingest corpus/seed/*.jsonl")
    parser.add_argument("--source", help="path to a source file")
    parser.add_argument("--collection", choices=[c.value for c in Collection],
                        help="target collection for --source")
    parser.add_argument("--format", default="jsonl", help="loader format (jsonl, markdown)")
    parser.add_argument("--grade", type=int, help="metadata: grade for --source")
    parser.add_argument("--subject", help="metadata: subject for --source")
    args = parser.parse_args(argv)

    ingestor = _build_default_ingestor()

    if args.seed:
        report = ingest_seed(ingestor)
        print(f"seed ingested: {report.documents} docs → {report.chunks} chunks "
              f"from {len(report.sources)} sources")
        return 0

    if args.source:
        if not args.collection:
            parser.error("--collection is required with --source")
        extra: dict[str, Any] = {}
        if args.grade is not None:
            extra["grade"] = args.grade
        if args.subject:
            extra["subject"] = args.subject
        report = ingestor.ingest_source(
            args.source, Collection(args.collection), fmt=args.format, extra_metadata=extra
        )
        print(f"ingested {report.documents} docs → {report.chunks} chunks "
              f"into {report.collection.value}")
        return 0

    parser.error("nothing to do: pass --seed or --source")
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
