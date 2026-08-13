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
from typing import Any

from .documents import Document

# Recognized JSONL record fields (shared by the file loader and the in-memory
# record path the corpus UI posts through). ``metadata`` fields may sit at the
# top level or inside a nested ``metadata`` object; both are merged.
_META_FIELDS: tuple[str, ...] = ("grade", "subject", "standard", "language", "topic", "framework")
_KNOWN: set[str] = {"id", "text", "source", "collection", "metadata", *_META_FIELDS}


def document_from_record(
    obj: Any, *, source_default: str, id_default: str
) -> Document:
    """Convert one JSONL-style record dict into a :class:`Document`.

    Shared by :class:`JsonlLoader` (reading a file) and the corpus UI endpoint
    (records posted in-memory), so both apply identical field/metadata rules and
    the same ``text`` validation. Unrecognized top-level scalars are preserved as
    metadata (forward-compatible)."""
    if not isinstance(obj, dict):
        # ValueError (not TypeError) on purpose: a non-object line is bad *input*,
        # so callers map it to a file-position error / HTTP 422, not a crash.
        raise ValueError("record must be a JSON object")  # noqa: TRY004
    if "text" not in obj or not str(obj["text"]).strip():
        raise ValueError("record missing non-empty 'text'")
    metadata: dict[str, Any] = dict(obj.get("metadata") or {})
    for f in _META_FIELDS:
        if f in obj:
            metadata[f] = obj[f]
    if "collection" in obj:
        metadata.setdefault("collection", obj["collection"])
    for k, v in obj.items():
        if k not in _KNOWN and not isinstance(v, (dict, list)):
            metadata[k] = v
    source = obj.get("source") or source_default
    doc_id = str(obj.get("id") or id_default)
    return Document(id=doc_id, text=str(obj["text"]).strip(), source=source, metadata=metadata)


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
                try:
                    yield document_from_record(
                        obj, source_default=f"{p.name}:{lineno}", id_default=f"{p.stem}:{lineno}"
                    )
                except ValueError as exc:
                    raise ValueError(f"{p}:{lineno}: {exc}") from exc


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
