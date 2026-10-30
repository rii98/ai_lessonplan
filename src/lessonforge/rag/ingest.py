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
import hashlib
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..providers.base import Embedder, SparseEmbedder, VectorRecord, VectorStore
from .chunkers import CHUNKER_REGISTRY, Chunker, ParagraphChunker, build_chunker
from .documents import DOC_ID_KEY, GLOBAL_SPACE, SPACE_KEY, Chunk, Collection, Document
from .loaders import build_loader, document_from_record

if TYPE_CHECKING:
    from ..domain.vocabulary import Vocabulary
    from ..knowledge.registry import KnowledgeRegistry


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
    space: str = GLOBAL_SPACE
    doc_ids: list[str] = field(default_factory=list)

    def merge(self, other: IngestReport) -> None:
        self.documents += other.documents
        self.chunks += other.chunks
        self.sources |= other.sources
        self.doc_ids.extend(other.doc_ids)


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
        vocabulary: Vocabulary | None = None,
        registry: KnowledgeRegistry | None = None,
    ) -> None:
        self.embedder = embedder
        # canonicalise grade/subject (unknown subject → ValueError → HTTP 422) so
        # retrieval filters match what Edboox context produces (D9).
        self.vocabulary = vocabulary
        # records one corpus_sources row per document (§16) when wired.
        self.registry = registry
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
        space: str = GLOBAL_SPACE,
        ingested_by: str | None = None,
    ) -> IngestReport:
        """Chunk, embed and upsert ``docs`` into ``space`` (default: the shared
        ``global`` shelf). The space is stamped on every chunk by ingestion alone —
        a ``_space`` key in record metadata is discarded, never trusted."""
        from ..knowledge.scope import valid_space

        if not valid_space(space):
            raise ValueError(f"invalid knowledge space {space!r}")
        chunker = chunker or self.chunker
        report = IngestReport(collection=collection, space=space)
        # group chunks by their actual target collection so a mixed file (records
        # that declare their own `collection`) lands in the right collections.
        by_collection: dict[Collection, list[Chunk]] = {}
        sources: dict[tuple[str, str], dict[str, Any]] = {}
        for doc in docs:
            report.documents += 1
            report.sources.add(doc.source)
            target = _coerce_collection(doc.metadata.get("collection"), collection)
            meta = {k: v for k, v in doc.metadata.items() if k != SPACE_KEY}
            if self.vocabulary is not None:
                meta = self.vocabulary.normalize_metadata(meta)
            # Stamp the document id on every chunk so hierarchy dereferences
            # (section/chapter expansion) are scoped to this book — identical
            # headings across different books never bleed together. An explicit
            # doc_id in the record's metadata wins; else the Document's own id. A
            # school's doc ids are namespaced by its space so they never collide
            # with the shared shelf's.
            doc_id = str(meta.pop(DOC_ID_KEY, doc.id))
            if space != GLOBAL_SPACE:
                doc_id = f"{space}/{doc_id}"
            meta = {**meta, DOC_ID_KEY: doc_id, SPACE_KEY: space}
            chunks = chunker.chunk(doc.text, collection=target, source=doc.source, metadata=meta)
            by_collection.setdefault(target, []).extend(chunks)
            entry = sources.setdefault((target.value, doc_id), {
                "title": str(meta.get("title") or doc.source)[:300], "source": doc.source,
                "sha": hashlib.sha256(), "chunks": 0})
            entry["sha"].update(doc.text.encode("utf-8"))
            entry["chunks"] += len(chunks)
            report.doc_ids.append(doc_id)

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

        if self.registry is not None and sources:
            from ..knowledge.registry import CorpusSource

            self.registry.ensure_space(space)
            self.registry.record_sources([
                CorpusSource(space_id=space, collection=col, doc_id=doc_id, title=e["title"],
                             source_label=e["source"], checksum=e["sha"].hexdigest(),
                             chunks=e["chunks"], ingested_by=ingested_by)
                for (col, doc_id), e in sources.items()
            ])
        return report

    def ingest_source(
        self,
        path: str | Path,
        collection: Collection,
        *,
        fmt: str = "jsonl",
        extra_metadata: dict[str, Any] | None = None,
        chunker: Chunker | None = None,
        space: str = GLOBAL_SPACE,
        ingested_by: str | None = None,
    ) -> IngestReport:
        loader = build_loader(fmt)
        docs = list(loader.load(path))
        if extra_metadata:
            for d in docs:
                d.metadata = {**extra_metadata, **d.metadata}
        # explicit chunker wins; else the policy picks by format; else the default.
        chosen = chunker or (self.chunker_policy.for_format(fmt) if self.chunker_policy else None)
        return self.ingest_documents(collection, docs, chunker=chosen, space=space,
                                     ingested_by=ingested_by)

    def ingest_records(
        self,
        collection: Collection,
        records: list[dict[str, Any]],
        *,
        source_label: str = "upload",
        space: str = GLOBAL_SPACE,
        ingested_by: str | None = None,
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
        return self.ingest_documents(collection, docs, space=space, ingested_by=ingested_by)


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


def ingest_seed(ingestor: Ingestor, seed_dir: Path | None = None, *,
                space: str = GLOBAL_SPACE) -> IngestReport:
    """Ingest ``corpus/seed/<collection>.jsonl`` for every collection that exists."""
    seed_dir = seed_dir or default_seed_dir()
    total = IngestReport(collection=Collection.curriculum)  # collection field is nominal here
    for collection in Collection:
        path = seed_dir / f"{collection.value}.jsonl"
        if path.exists():
            total.merge(ingestor.ingest_source(path, collection, fmt="jsonl", space=space))
    return total


def _build_default_ingestor() -> Ingestor:
    # local import so the module imports cheaply (no provider construction) in tests.
    # The container's ingestor carries the vocabulary (subject/grade normalisation)
    # and the knowledge registry (corpus_sources rows), exactly like the API path.
    from ..container import Container

    ingestor = Container.from_settings().ingestor
    assert ingestor is not None
    return ingestor


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
    parser.add_argument("--space", default=GLOBAL_SPACE,
                        help="knowledge space: 'global' (every school) or 'school:<id>'")
    args = parser.parse_args(argv)

    ingestor = _build_default_ingestor()

    if args.seed:
        report = ingest_seed(ingestor, space=args.space)
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
            extra_metadata=extra, chunker=override, space=args.space,
        )
        print(f"ingested {report.documents} docs → {report.chunks} chunks "
              f"into {report.collection.value}")
        return 0

    parser.error("nothing to do: pass --seed or --source")
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
