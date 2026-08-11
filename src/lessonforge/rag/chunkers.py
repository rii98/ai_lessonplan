"""Chunkers: a :class:`Document` → a list of embeddable :class:`Chunk`.

Behind an ABC so the strategy is swappable (sentence-window, token-count, or a
semantic splitter later) without touching the ingestor. The default splits on
blank lines and greedily packs paragraphs up to a character budget, so short
records (a single misconception, one local-context entry) stay whole while long
exemplars are divided at natural boundaries.

Chunking is deterministic — same document, same chunks, same content-hash ids —
which keeps ingestion idempotent and tests byte-stable.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from .documents import Chunk, Collection, content_id


class Chunker(ABC):
    @abstractmethod
    def split(self, text: str) -> list[str]:
        """Split raw text into chunk-sized strings."""

    def chunk(
        self,
        text: str,
        *,
        collection: Collection,
        source: str,
        metadata: dict[str, Any] | None = None,
    ) -> list[Chunk]:
        metadata = dict(metadata or {})
        metadata.pop("collection", None)  # collection is a first-class field, not payload noise
        out: list[Chunk] = []
        for part in self.split(text):
            part = part.strip()
            if not part:
                continue
            out.append(
                Chunk(
                    id=content_id(collection, part, metadata),
                    text=part,
                    collection=collection,
                    source=source,
                    metadata=metadata,
                )
            )
        return out


class ParagraphChunker(Chunker):
    """Greedy paragraph packer. Splits on blank lines, then merges consecutive
    paragraphs until adding the next would exceed ``max_chars``. Never splits a
    single paragraph that already fits."""

    def __init__(self, *, max_chars: int = 800, min_chars: int = 1) -> None:
        if max_chars < 1:
            raise ValueError("max_chars must be >= 1")
        self.max_chars = max_chars
        self.min_chars = min_chars

    def split(self, text: str) -> list[str]:
        paras = [p.strip() for p in text.replace("\r\n", "\n").split("\n\n") if p.strip()]
        chunks: list[str] = []
        buf = ""
        for para in paras:
            if not buf:
                buf = para
            elif len(buf) + 2 + len(para) <= self.max_chars:
                buf = f"{buf}\n\n{para}"
            else:
                chunks.append(buf)
                buf = para
        if buf:
            chunks.append(buf)
        # drop trivially short fragments unless that would discard everything
        kept = [c for c in chunks if len(c) >= self.min_chars]
        return kept or chunks
