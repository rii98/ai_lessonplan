"""Chunkers: a :class:`Document` → a list of embeddable :class:`Chunk`.

Behind an ABC so the strategy is swappable (paragraph packing, structure-aware
markdown, a semantic splitter later) without touching the ingestor. A chunker
turns raw text into :class:`TextPart` fragments — each fragment carrying its own
extra metadata — and the shared :meth:`Chunker.chunk` stamps the collection,
source, and content-hash id onto every one. Per-fragment metadata is what lets a
structure-aware chunker attach a *different* header breadcrumb to each chunk while
a flat chunker attaches none.

Chunking is deterministic — same document, same chunks, same content-hash ids —
which keeps ingestion idempotent and tests byte-stable.

Chunkers self-register under a name (mirroring the loader/provider registries), so
selecting one is config, not code: see :func:`build_chunker` and the ``chunking``
config block.
"""

from __future__ import annotations

import inspect
import re
from abc import ABC, abstractmethod
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .documents import (
    CHAPTER_KEY,
    CHAPTER_LEVEL_KEY,
    CHUNK_INDEX_KEY,
    Chunk,
    Collection,
    content_id,
)


@dataclass(slots=True)
class TextPart:
    """One fragment of a split document, plus any metadata specific to it (e.g. a
    markdown chunk's heading breadcrumb). Merged over the document's base metadata
    by :meth:`Chunker.chunk`, so per-part keys win."""

    text: str
    metadata: dict[str, Any] = field(default_factory=dict)


class Chunker(ABC):
    @abstractmethod
    def parts(self, text: str) -> list[TextPart]:
        """Split raw text into fragments, each with its own extra metadata."""

    def chunk(
        self,
        text: str,
        *,
        collection: Collection,
        source: str,
        metadata: dict[str, Any] | None = None,
    ) -> list[Chunk]:
        base = dict(metadata or {})
        base.pop("collection", None)  # collection is a first-class field, not payload noise
        out: list[Chunk] = []
        for part in self.parts(text):
            body = part.text.strip()
            if not body:
                continue
            # per-part metadata (breadcrumb) wins; ``chunk_index`` is the chunk's
            # position in document order — the sort key that reassembles a section
            # or chapter, and part of the content id so two identical passages at
            # different positions stay distinct points instead of colliding.
            meta = {**base, **part.metadata, CHUNK_INDEX_KEY: len(out)}
            out.append(
                Chunk(
                    id=content_id(collection, body, meta),
                    text=body,
                    collection=collection,
                    source=source,
                    metadata=meta,
                )
            )
        return out


class StringChunker(Chunker):
    """Base for chunkers that only split text and attach no per-chunk metadata.
    Subclasses implement :meth:`split`; :meth:`parts` wraps each string."""

    @abstractmethod
    def split(self, text: str) -> list[str]:
        """Split raw text into chunk-sized strings."""

    def parts(self, text: str) -> list[TextPart]:
        return [TextPart(s) for s in self.split(text)]


# ── registry ─────────────────────────────────────────────────────────────────
CHUNKER_REGISTRY: dict[str, type[Chunker]] = {}


def register_chunker(name: str) -> Callable[[type[Chunker]], type[Chunker]]:
    def deco(cls: type[Chunker]) -> type[Chunker]:
        if name in CHUNKER_REGISTRY:
            raise ValueError(f"chunker {name!r} already registered as {CHUNKER_REGISTRY[name]!r}")
        cls.name = name  # type: ignore[attr-defined]
        CHUNKER_REGISTRY[name] = cls
        return cls

    return deco


def build_chunker(name: str, **params: Any) -> Chunker:
    """Resolve a chunker by name and construct it with ``params`` (from config).

    Params the chunker's constructor doesn't accept are silently dropped, so a
    single shared ``chunking.params`` block can hold knobs for several chunkers
    (e.g. ``max_chars`` for both, ``split_levels`` for markdown only) without a
    format's chunker choking on another's keys."""
    try:
        cls = CHUNKER_REGISTRY[name]
    except KeyError:
        available = ", ".join(sorted(CHUNKER_REGISTRY)) or "<none registered>"
        raise ValueError(
            f"Unknown chunker {name!r}. Available: {available}."
        ) from None
    sig = inspect.signature(cls.__init__)
    accepted = {k: v for k, v in params.items() if k in sig.parameters}
    return cls(**accepted)


def _greedy_pack(paras: list[str], max_chars: int) -> list[str]:
    """Merge consecutive paragraphs until adding the next would exceed ``max_chars``;
    never splits a single paragraph that already fits. Shared by the paragraph and
    markdown chunkers."""
    chunks: list[str] = []
    buf = ""
    for para in paras:
        if not buf:
            buf = para
        elif len(buf) + 2 + len(para) <= max_chars:
            buf = f"{buf}\n\n{para}"
        else:
            chunks.append(buf)
            buf = para
    if buf:
        chunks.append(buf)
    return chunks


@register_chunker("paragraph")
class ParagraphChunker(StringChunker):
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
        chunks = _greedy_pack(paras, self.max_chars)
        # drop trivially short fragments unless that would discard everything
        kept = [c for c in chunks if len(c) >= self.min_chars]
        return kept or chunks


# ── structure-aware markdown ─────────────────────────────────────────────────
_ATX_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_FENCE_RE = re.compile(r"^\s*(```+|~~~+)")


@dataclass(slots=True)
class _Section:
    """A heading and the body text directly under it (before the next heading),
    with its ancestor breadcrumb resolved from the heading stack."""

    level: int          # 0 = preamble (text before any heading)
    title: str
    breadcrumb: list[str]   # ancestor titles, nearest-last (excludes ``title``)
    body: str
    crumb_levels: list[int] = field(default_factory=list)  # markdown level of each breadcrumb entry, aligned to ``breadcrumb``


@register_chunker("markdown")
class MarkdownChunker(Chunker):
    """Structure-aware markdown splitter that preserves the header hierarchy.

    A document is parsed into sections at ATX headers (``#``…``######``), skipping
    ``#`` inside fenced code blocks. Each section becomes one or more chunks that
    carry the *full breadcrumb* of its ancestor headings as metadata
    (``heading_path``, ``heading``, ``heading_level``) — so a retrieved chunk knows
    it came from "Force > Newton's Laws > First Law", and that path is filterable.

    Adjustable knobs (all optional):

    - ``max_chars`` — hard cap; a section longer than this is paragraph-packed into
      several chunks that *share* the section's breadcrumb.
    - ``min_chars`` — merge threshold; consecutive short sections under the same
      immediate parent are concatenated into one chunk (default ``1`` = disabled,
      so behavior is predictable out of the box).
    - ``split_levels`` — which header depths begin a new section (default all,
      ``1..6``); a header at a level *not* in this set is folded into its parent's
      body as an inline sub-heading, so you can chunk at chapter/section
      granularity without shattering on every sub-sub-heading.
    - ``prepend_breadcrumb`` — prefix each chunk's text with its heading path so
      the embedding captures where the passage sits.
    - ``include_heading`` — keep the section's own heading line in the body text.
    - ``chapter_level`` — the heading depth that counts as a *chapter* in this book
      (the ``CHAPTER_KEY`` bucket the coverage outline enumerates). Books disagree:
      a Science book's chapter is its ``# Unit N`` (level 1), a Math book's is a
      ``## Chapter N`` (level 2) nested under a ``# Unit`` grouping. Left ``None``
      (default), it is *auto-detected* per document from the headings — a
      ``Chapter N`` heading wins, else a ``Unit/Lesson N`` heading, else level 1 —
      and the resolved value is stamped on every chunk (``chapter_level``) so
      retrieval reads it back rather than assuming one shape for the whole store.
      Pass an int to override detection for an oddly-headed book.
    """

    def __init__(
        self,
        *,
        max_chars: int = 1200,
        min_chars: int = 1,
        split_levels: tuple[int, ...] = (1, 2, 3, 4, 5, 6),
        prepend_breadcrumb: bool = True,
        include_heading: bool = True,
        breadcrumb_sep: str = " > ",
        chapter_level: int | None = None,
    ) -> None:
        if max_chars < 1:
            raise ValueError("max_chars must be >= 1")
        if chapter_level is not None and chapter_level < 1:
            raise ValueError("chapter_level must be >= 1")
        self.max_chars = max_chars
        self.min_chars = min_chars
        self.split_levels = frozenset(split_levels)
        self.prepend_breadcrumb = prepend_breadcrumb
        self.include_heading = include_heading
        self.breadcrumb_sep = breadcrumb_sep
        self.chapter_level = chapter_level

    def parts(self, text: str) -> list[TextPart]:
        sections = self._sections(text.replace("\r\n", "\n"))
        if self.min_chars > 1:
            sections = self._merge_small(sections)
        chapter_level = self._resolve_chapter_level(sections)
        out: list[TextPart] = []
        for sec in sections:
            body = sec.body.strip()
            if not body:
                continue  # a heading with no content of its own; lives on in descendants' breadcrumb
            path = [*sec.breadcrumb, sec.title] if sec.title else list(sec.breadcrumb)
            heading_path = self.breadcrumb_sep.join(path)
            meta: dict[str, Any] = {}
            if heading_path:
                meta["heading_path"] = heading_path
            # The chapter is the ancestor (or self) heading at chapter granularity —
            # the deepest one whose markdown level is at or above ``chapter_level`` —
            # NOT blindly the outermost heading, so a ``## Chapter 11`` nested under a
            # ``# Unit`` grouping still buckets per chapter instead of per unit.
            chapter = self._chapter_of(sec, chapter_level)
            if chapter:
                meta[CHAPTER_KEY] = chapter
                meta[CHAPTER_LEVEL_KEY] = chapter_level
            elif path:  # section sits entirely above chapter granularity → fall back
                meta[CHAPTER_KEY] = path[0]
                meta[CHAPTER_LEVEL_KEY] = chapter_level
            if sec.title:
                meta["heading"] = sec.title
                meta["heading_level"] = sec.level
            for body_chunk in _greedy_pack(_paragraphs(body), self.max_chars):
                out.append(TextPart(self._compose(heading_path, sec, body_chunk), dict(meta)))
        return out

    @staticmethod
    def _chapter_of(sec: _Section, chapter_level: int) -> str | None:
        """The chapter title for a section: the deepest heading in its
        ancestor→self chain whose markdown level is ``<= chapter_level``. Returns
        ``None`` for a section that sits entirely deeper than any chapter heading
        (e.g. a stray sub-section with a skipped chapter level) — the caller then
        falls back to the outermost heading."""
        chain = list(zip(sec.crumb_levels, sec.breadcrumb))
        if sec.title:
            chain.append((sec.level, sec.title))
        chapter: str | None = None
        for lvl, title in chain:
            if lvl <= chapter_level:
                chapter = title  # keep the deepest (last) at-or-above chapter level
        return chapter

    def _resolve_chapter_level(self, sections: list[_Section]) -> int:
        """The heading level that counts as a chapter in this document: the
        explicit override when given, else auto-detected from the headings."""
        if self.chapter_level is not None:
            return self.chapter_level
        # Inventory every heading (section titles AND bodyless ancestors that appear
        # only in breadcrumbs — a ``## Chapter 11`` whose content lives under 11.x
        # has no body of its own, so it survives only in its children's crumbs).
        headings: dict[tuple[int, str], None] = {}
        for sec in sections:
            for lvl, title in zip(sec.crumb_levels, sec.breadcrumb):
                headings[(lvl, title)] = None
            if sec.title:
                headings[(sec.level, sec.title)] = None
        return _detect_chapter_level(headings.keys())

    def _compose(self, heading_path: str, sec: _Section, body: str) -> str:
        prefix = ""
        if self.prepend_breadcrumb and heading_path:
            prefix += f"[{heading_path}]\n"
        elif self.include_heading and sec.title:
            prefix += f"{'#' * max(sec.level, 1)} {sec.title}\n"
        return f"{prefix}{body}" if prefix else body

    def _sections(self, text: str) -> list[_Section]:
        sections: list[_Section] = []
        stack: list[tuple[int, str]] = []   # (level, title) of open ancestor headings
        cur_level, cur_title, cur_bc, cur_bc_levels = 0, "", [], []
        buf: list[str] = []
        in_fence = False

        def flush() -> None:
            sections.append(
                _Section(cur_level, cur_title, list(cur_bc), "\n".join(buf), list(cur_bc_levels))
            )

        for line in text.split("\n"):
            if _FENCE_RE.match(line):
                in_fence = not in_fence
                buf.append(line)
                continue
            m = None if in_fence else _ATX_RE.match(line)
            if m and len(m.group(1)) in self.split_levels:
                flush()
                level = len(m.group(1))
                # pop ancestors at or below this level, then this heading's breadcrumb
                # is whatever remains on the stack.
                while stack and stack[-1][0] >= level:
                    stack.pop()
                cur_bc = [t for _, t in stack]
                cur_bc_levels = [lv for lv, _ in stack]
                stack.append((level, m.group(2).strip()))
                cur_level, cur_title, buf = level, m.group(2).strip(), []
            else:
                buf.append(line)
        flush()
        # drop an empty preamble section (no title, no body)
        return [s for s in sections if s.title or s.body.strip()]

    def _merge_small(self, sections: list[_Section]) -> list[_Section]:
        """Concatenate consecutive short sections that share the same immediate
        parent breadcrumb, so a page of one-line sub-sections becomes coherent
        chunks instead of noise. Only bodies under ``min_chars`` are folded, and
        only up to ``max_chars``."""
        merged: list[_Section] = []
        for sec in sections:
            prev = merged[-1] if merged else None
            if (
                prev is not None
                and prev.breadcrumb == sec.breadcrumb
                and len(prev.body) < self.min_chars
                and len(prev.body) + len(sec.body) + 2 <= self.max_chars
            ):
                title = f"{prev.title} · {sec.title}".strip(" ·")
                body = f"{prev.body}\n\n#{'#' * sec.level} {sec.title}\n{sec.body}".strip()
                merged[-1] = _Section(
                    prev.level, title or prev.title, prev.breadcrumb, body, prev.crumb_levels
                )
            else:
                merged.append(sec)
        return merged


def _paragraphs(text: str) -> list[str]:
    return [p.strip() for p in text.split("\n\n") if p.strip()]


# A chapter heading names itself: "Chapter 11", "Chapter 11: Exponential Equation".
# A unit/lesson heading is the fallback chapter marker for books with no explicit
# "Chapter" level (e.g. a Science book whose teachable chapter is "Unit 11 : Wave").
_CHAPTER_RE = re.compile(r"(?i)^\s*chapter\b")
_UNIT_RE = re.compile(r"(?i)^\s*(?:unit|lesson)\b")


def _detect_chapter_level(headings: Any) -> int:
    """Infer the markdown level that means "chapter" from a book's headings.

    Precedence, so a book that has BOTH a unit grouping and chapters resolves to
    the chapter (the finer, correct bucket): an explicit ``Chapter N`` heading
    wins; else a ``Unit/Lesson N`` heading; else level 1 (the whole-book fallback,
    which is today's behaviour). When several headings match, the most common level
    wins so one stray heading can't move the level.

    ``headings`` is an iterable of ``(level, title)`` pairs — every heading in the
    document, including bodyless ancestors that survive only in breadcrumbs."""
    chapter_levels: Counter[int] = Counter()
    unit_levels: Counter[int] = Counter()
    for level, title in headings:
        if _CHAPTER_RE.match(title):
            chapter_levels[level] += 1
        elif _UNIT_RE.match(title):
            unit_levels[level] += 1
    if chapter_levels:
        return chapter_levels.most_common(1)[0][0]
    if unit_levels:
        return unit_levels.most_common(1)[0][0]
    return 1
