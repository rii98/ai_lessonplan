"""Pluggable corpus loaders: a raw source → :class:`Document` stream.

Same loose-coupling pattern as the provider registry: a loader self-registers
under a format string, and :func:`build_loader` resolves it. Adding a new source
format (option 2 in the M2 plan — raw CDC PDFs, with OCR for scanned pages) is a
new class plus a one-line ``@register_loader`` decorator; nothing downstream
(chunker, ingestor, CLI) changes.

Shipped now:
- ``jsonl`` — one JSON object per line (the seed corpus format).
- ``markdown`` — a whole ``.md`` file as a single document (e.g. an exemplar
  lesson); the chunker splits it. Proves the seam without needing PDFs yet.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import Any, ClassVar

from .documents import Document

LOADER_REGISTRY: dict[str, type[Loader]] = {}


def register_loader(fmt: str) -> Callable[[type[Loader]], type[Loader]]:
    def deco(cls: type[Loader]) -> type[Loader]:
        if fmt in LOADER_REGISTRY:
            raise ValueError(f"loader {fmt!r} already registered as {LOADER_REGISTRY[fmt]!r}")
        cls.fmt = fmt
        LOADER_REGISTRY[fmt] = cls
        return cls

    return deco


def build_loader(fmt: str) -> Loader:
    try:
        return LOADER_REGISTRY[fmt]()
    except KeyError:
        available = ", ".join(sorted(LOADER_REGISTRY)) or "<none registered>"
        raise ValueError(
            f"Unknown loader format {fmt!r}. Available: {available}."
        ) from None


class Loader(ABC):
    fmt: str

    @abstractmethod
    def load(self, path: str | Path) -> Iterable[Document]:
        """Yield source documents from ``path``."""


@register_loader("jsonl")
class JsonlLoader(Loader):
    """One JSON object per line. Recognized fields::

        {"id"?, "text", "source"?, "collection"?, "grade"?, "subject"?,
         "standard"?, "language"?, "metadata"? {...}}

    ``text`` is required. Any recognized metadata field may sit at the top level
    or inside a nested ``metadata`` object; both are merged. ``id``/``source``
    default sensibly so a minimal ``{"text": ...}`` line still loads.
    """

    _META_FIELDS: ClassVar[tuple[str, ...]] = ("grade", "subject", "standard", "language", "topic")
    _KNOWN: ClassVar[set[str]] = {"id", "text", "source", "collection", "metadata", *_META_FIELDS}

    def load(self, path: str | Path) -> Iterator[Document]:
        p = Path(path)
        with p.open(encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, 1):
                line = line.strip()
                if not line or line.startswith("//"):
                    continue
                try:
                    obj: dict[str, Any] = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"{p}:{lineno}: invalid JSON: {exc}") from exc
                if "text" not in obj or not str(obj["text"]).strip():
                    raise ValueError(f"{p}:{lineno}: record missing non-empty 'text'")
                yield self._to_doc(obj, p, lineno)

    def _to_doc(self, obj: dict[str, Any], p: Path, lineno: int) -> Document:
        metadata: dict[str, Any] = dict(obj.get("metadata") or {})
        for f in self._META_FIELDS:
            if f in obj:
                metadata[f] = obj[f]
        if "collection" in obj:
            metadata.setdefault("collection", obj["collection"])
        # keep any unrecognized top-level scalars as metadata too (forward-compatible)
        for k, v in obj.items():
            if k not in self._KNOWN and not isinstance(v, (dict, list)):
                metadata[k] = v
        source = obj.get("source") or f"{p.name}:{lineno}"
        doc_id = str(obj.get("id") or f"{p.stem}:{lineno}")
        return Document(id=doc_id, text=str(obj["text"]).strip(), source=source, metadata=metadata)


@register_loader("markdown")
class MarkdownLoader(Loader):
    """Load a whole Markdown file as one document. The chunker splits it into
    sections; useful for ingesting an exemplar lesson (e.g. ``lp1.md``) directly.
    Metadata may be supplied out-of-band by the caller via the ingestor."""

    def load(self, path: str | Path) -> Iterator[Document]:
        p = Path(path)
        text = p.read_text(encoding="utf-8").strip()
        if not text:
            return
        yield Document(id=p.stem, text=text, source=p.name, metadata={})
