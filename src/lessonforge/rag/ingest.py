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

from ..providers.base import Embedder, SparseEmbedder, VectorRecord, VectorStore
from .chunkers import CHUNKER_REGISTRY, Chunker, ParagraphChunker, build_chunker
from .documents import DOC_ID_KEY, Chunk, Collection, Document
from .loaders import build_loader, document_from_record


class ChunkerPolicy:
    """Resolves which :class:`Chunker` to use for a given source format.

    Built from the ``chunking`` config block, it mirrors the loader/provider
    registries' loose coupling: the ingestor never names a concrete chunker, it
    asks the policy, and swapping "markdown → structure-aware" is one config line.
    Instances are cached per format so repeated ingests don't rebuild them."""

    def __init__(
        self,
        *,
        default: str = "paragraph",
        by_format: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
    ) -> None:
        self.default = default
        self.by_format = dict(by_format or {})
        self.params = dict(params or {})
        self._cache: dict[str, Chunker] = {}

    @classmethod
    def from_config(cls, config: Any) -> ChunkerPolicy:
        return cls(default=config.default, by_format=config.by_format, params=config.params)

    def for_format(self, fmt: str) -> Chunker:
        name = self.by_format.get(fmt, self.default)
        if name not in self._cache:
            self._cache[name] = build_chunker(name, **self.params)
        return self._cache[name]


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
        chunker_policy: ChunkerPolicy | None = None,
        sparse_embedder: SparseEmbedder | None = None,
        hybrid: bool = False,
        batch_size: int = 64,
    ) -> None:
        self.embedder = embedder
        self.vector_store = vector_store
        # ``chunker`` is the fallback default; ``chunker_policy`` selects per source
        # format when set (e.g. markdown → structure-aware). Either may be omitted.
        self.chunker = chunker or ParagraphChunker()
        self.chunker_policy = chunker_policy
        self.sparse_embedder = sparse_embedder
        self.hybrid = hybrid
        self.batch_size = batch_size

    @property
    def hybrid_active(self) -> bool:
        """Ingest sparse vectors only when asked for AND a sparse encoder is wired;
        so the collection is created hybrid-ready and queries can fuse."""
        return bool(self.hybrid and self.sparse_embedder is not None)

    # ── core ────────────────────────────────────────────────────────────────
    def ingest_documents(
        self,
        collection: Collection,
        docs: Iterable[Document],
        *,
        chunker: Chunker | None = None,
    ) -> IngestReport:
        chunker = chunker or self.chunker
        report = IngestReport(collection=collection)
        # group chunks by their actual target collection so a mixed file (records
        # that declare their own `collection`) lands in the right collections.
        by_collection: dict[Collection, list[Chunk]] = {}
        for doc in docs:
            report.documents += 1
            report.sources.add(doc.source)
            target = _coerce_collection(doc.metadata.get("collection"), collection)
            # Stamp the document id on every chunk so hierarchy dereferences
            # (section/chapter expansion) are scoped to this book — identical
            # headings across different books never bleed together. An explicit
            # doc_id in the record's metadata wins; else the Document's own id.
            meta = {DOC_ID_KEY: doc.id, **doc.metadata}
            by_collection.setdefault(target, []).extend(
                chunker.chunk(
                    doc.text, collection=target, source=doc.source, metadata=meta
                )
            )

        hybrid = self.hybrid_active
        for target, chunks in by_collection.items():
            if not chunks:
                continue
            self.vector_store.ensure_collection(target.value, self.embedder.dim, sparse=hybrid)
            for batch in _batched(chunks, self.batch_size):
                texts = [c.text for c in batch]
                vectors = self.embedder.embed(texts)
                sparse = (
                    self.sparse_embedder.embed_sparse(texts)  # type: ignore[union-attr]
                    if hybrid else [None] * len(batch)
                )
                records = [
                    VectorRecord(id=c.id, vector=v, payload=c.payload(), sparse_vector=s)
                    for c, v, s in zip(batch, vectors, sparse, strict=True)
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
        chunker: Chunker | None = None,
    ) -> IngestReport:
        loader = build_loader(fmt)
        docs = list(loader.load(path))
        if extra_metadata:
            for d in docs:
                d.metadata = {**extra_metadata, **d.metadata}
        # explicit chunker wins; else the policy picks by format; else the default.
        chosen = chunker or (self.chunker_policy.for_format(fmt) if self.chunker_policy else None)
        return self.ingest_documents(collection, docs, chunker=chosen)

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
    return Ingestor(
        embedder=c.embedder,
        vector_store=c.vector_store,
        chunker_policy=ChunkerPolicy.from_config(c.settings.chunking),
        sparse_embedder=c.sparse_embedder,
        hybrid=c.settings.retrieval.hybrid,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ingest a corpus into the vector store.")
    parser.add_argument("--seed", action="store_true", help="ingest corpus/seed/*.jsonl")
    parser.add_argument("--source", help="path to a source file")
    parser.add_argument("--collection", choices=[c.value for c in Collection],
                        help="target collection for --source")
    parser.add_argument("--format", default="jsonl", help="loader format (jsonl, markdown)")
    parser.add_argument("--chunker", choices=sorted(CHUNKER_REGISTRY),
                        help="override the chunker (default: per-format from config)")
    parser.add_argument("--max-chars", type=int, dest="max_chars",
                        help="chunk size budget for the chosen chunker")
    parser.add_argument("--chapter-level", type=int, dest="chapter_level",
                        help="markdown heading level that means 'chapter' for this "
                             "book (1 = '# Unit', 2 = '## Chapter' under a unit); "
                             "default: auto-detected per document")
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
        # an explicit --chunker (optionally with --max-chars / --chapter-level)
        # overrides the policy. --chapter-level forces the markdown chunker even
        # without --chunker, since it only makes sense there.
        override: Chunker | None = None
        if args.chunker or args.chapter_level is not None:
            params: dict[str, Any] = {}
            if args.max_chars:
                params["max_chars"] = args.max_chars
            if args.chapter_level is not None:
                params["chapter_level"] = args.chapter_level
            override = build_chunker(args.chunker or "markdown", **params)
        report = ingestor.ingest_source(
            args.source, Collection(args.collection), fmt=args.format,
            extra_metadata=extra, chunker=override,
        )
        print(f"ingested {report.documents} docs → {report.chunks} chunks "
              f"into {report.collection.value}")
        return 0

    parser.error("nothing to do: pass --seed or --source")
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
