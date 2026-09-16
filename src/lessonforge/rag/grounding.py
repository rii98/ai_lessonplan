"""GroundingRetriever: assemble multi-collection grounding for enrichment.

Wraps the interface-only :class:`Retriever` and queries each configured
collection (curriculum, pedagogical, exemplar, local_context) with grade/subject
metadata filters, then returns a :class:`GroundingBundle` carrying:

- the retrieved chunks per collection,
- a de-duplicated, ordered list of provenance ``sources`` (→ the LDD's
  ``quality.grounding_sources``, so citations are real, not model-invented),
- a formatted ``context`` block ready to drop into the enrichment prompt.

Which collections are queried, how many chunks each contributes, and which brief
fields become filters are all config-driven (``grounding`` block) — swapping the
grounding strategy is a config edit, not a code change.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..domain.unit import UnitPlan

_BREADCRUMB_SEP = " > "  # must match MarkdownChunker.breadcrumb_sep
# Leading section numbering to strip when comparing a heading to plan text:
# "12", "12.1", "12.1.", "a.", "b)", "iv." — so "12.1 The Universe" matches a day
# titled "The Universe". Applied only for MATCHING, never to the displayed label.
_NUMBERING_RE = re.compile(r"^\s*(?:\d+(?:\.\d+)*\.?|[a-zA-Z]|[ivxIVX]+)[.)]\s+")
_STOPWORDS = frozenset(
    "the a an of to in on and or for with without into from by as is are be its "
    "this that these those study regarding".split()
)


def _clean_heading(path: str) -> str:
    """Strip Markdown emphasis/heading noise from a heading path for DISPLAY —
    ``**12.1 Foo**`` → ``12.1 Foo`` — keeping the ``>`` breadcrumb and numbering
    (which read well in the syllabus contract). Not for matching."""
    parts = [re.sub(r"[*`_#]+", "", seg).strip() for seg in path.split(_BREADCRUMB_SEP)]
    return _BREADCRUMB_SEP.join(p for p in parts if p)


def _truncate(path: str, depth: int) -> str:
    """Keep the chapter (segment 0) plus ``depth`` levels below it, so a deep
    sub-heading collapses into its parent section — a section that exists only via
    its sub-headings is thus preserved, never dropped."""
    segs = path.split(_BREADCRUMB_SEP)
    return _BREADCRUMB_SEP.join(segs[: max(1, depth + 1)])


def _chapter_scoped(path: str, chapter_level: int) -> str:
    """Collapse a heading path to ``chapter > section`` for the coverage outline,
    using the book's own ``chapter_level`` (stamped at ingest).

    Keeps the chapter segment (at depth ``chapter_level``) and the one level below
    it — so any coarser grouping ABOVE the chapter (a "Unit IV: Algebra" wrapping
    "Chapter 11") is dropped, and any finer sub-heading below the section (an
    Example/Exercise/PART) folds into its section. A section that exists only via
    its sub-headings survives, because the slice never trims past the section
    level."""
    segs = path.split(_BREADCRUMB_SEP)
    start = max(0, chapter_level - 1)
    return _BREADCRUMB_SEP.join(segs[start : chapter_level + 1]) or path


def _match_tokens(text: str) -> set[str]:
    """Content words of a heading/topic, lowercased, numbering + emphasis + stop
    words removed — the unit of the lenient coverage comparison."""
    text = re.sub(r"[*`_#]+", "", text)
    text = _NUMBERING_RE.sub("", text).rstrip(":.").strip().lower()
    return {t for t in re.split(r"[^a-z0-9]+", text) if len(t) > 2 and t not in _STOPWORDS}

from ..config import GroundingConfig
from ..providers.base import LLMClient
from .documents import (
    CHAPTER_KEY,
    CHAPTER_LEVEL_KEY,
    CHUNK_INDEX_KEY,
    DOC_ID_KEY,
    HEADING_PATH_KEY,
    Collection,
)
from .retriever import Granularity, RetrievedChunk, Retriever

# Shown as the source when nothing was retrieved — so every document still quotes
# an honest provenance (the model's own knowledge) rather than claiming a citation
# it doesn't have, or leaving the field blank.
NO_SOURCE_MARKER = "Model general knowledge (no reference material found)"


def ensure_sources(sources: list[str]) -> list[str]:
    """Guarantee a non-empty, de-duplicated source list: real retrieved sources
    when present, else the honest 'model general knowledge' marker. This is what
    makes 'every document quotes its source' always satisfiable."""
    seen: dict[str, None] = {}
    for s in sources:
        s = str(s).strip()
        if s:
            seen.setdefault(s, None)
    return list(seen) or [NO_SOURCE_MARKER]


def merge_sources(existing: list[str], new: list[str]) -> list[str]:
    """Union of two source lists, order-preserving — citations only grow, never
    shrink or get fabricated (used when an AI edit re-grounds a document)."""
    merged = [s for s in existing if s and s != NO_SOURCE_MARKER]
    for s in new:
        s = str(s).strip()
        if s and s != NO_SOURCE_MARKER and s not in merged:
            merged.append(s)
    return merged or list(existing)


@dataclass(slots=True)
class GroundingBundle:
    chunks: dict[str, list[RetrievedChunk]] = field(default_factory=dict)
    sources: list[str] = field(default_factory=list)
    # Collections whose hits are authoritative course material (real textbook
    # content). Rendered first, under a "prefer this over general knowledge"
    # directive. Set by the retriever from config; empty here for standalone use.
    authoritative: frozenset[str] = field(default_factory=frozenset)

    @property
    def is_empty(self) -> bool:
        return not any(self.chunks.values())

    def _has(self, name: str) -> bool:
        return bool(self.chunks.get(name))

    @property
    def has_authoritative(self) -> bool:
        """True when real course material was retrieved for this topic — the signal
        that generation should ground in the book rather than model knowledge."""
        return any(self._has(n) for n in self.authoritative)

    def as_prompt_context(self) -> str:
        """Render the bundle as labeled context for the enrichment prompt.

        Authoritative collections (``reference`` — actual course material) are
        rendered first under a stronger directive so the model prefers the real
        book over its own general knowledge. When nothing was retrieved at all, the
        model is told to fall back to its curriculum knowledge — so a topic with no
        ingested book still generates, just ungrounded."""
        if self.is_empty:
            return "No retrieved context available; rely on curriculum knowledge."
        sections: list[str] = []

        auth = [(n, cs) for n, cs in self.chunks.items() if n in self.authoritative and cs]
        if auth:
            sections.append(
                "AUTHORITATIVE course material below — prefer it over your own "
                "general knowledge, ground your content in it, and cite these "
                "sources in quality.grounding_sources:"
            )
            for name, chunks in auth:
                sections.append(f"\n[{name}]")
                sections.extend(f"- {c.text}  (source: {c.payload.get('source', '?')})"
                                for c in chunks)

        other = [(n, cs) for n, cs in self.chunks.items() if n not in self.authoritative and cs]
        if other:
            sections.append(
                ("\nSupporting context (use it; cite these sources in "
                 "quality.grounding_sources):") if auth else
                "Grounding context (use it; cite these sources in quality.grounding_sources):"
            )
            for name, chunks in other:
                sections.append(f"\n[{name}]")
                sections.extend(f"- {c.text}  (source: {c.payload.get('source', '?')})"
                                for c in chunks)
        return "\n".join(sections)


@dataclass(slots=True)
class CoverageOutline:
    """The complete, ordered list of a chapter's sections — the coverage contract a
    unit plan must satisfy.

    Built by metadata scroll (:meth:`GroundingRetriever.skeleton`), NOT by vector
    similarity, so it is the whole table of contents rather than the handful of
    chunks a reranker happened to surface — no topic is silently dropped.
    ``chapters`` are the source chapter label(s) the sections were drawn from
    (usually one; more when a topic spans chapters)."""

    chapters: list[str] = field(default_factory=list)
    sections: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.sections

    def as_prompt_context(self) -> str:
        """Render the outline as a hard coverage contract for the planner prompt.
        Empty string when nothing was enumerated, so the caller can simply skip
        the block (the planner then falls back to ungrounded arc design)."""
        if self.is_empty:
            return ""
        head = (
            "SYLLABUS — the complete, in-order list of every section in this "
            "chapter. Your plan MUST cover EVERY one of them across the days: do "
            "not skip, silently merge away, or invent topics. A single day may "
            "cover several adjacent sections when there are more sections than "
            "days, but no section may be left out."
        )
        return "\n".join([head, *(f"- {s}" for s in self.sections)])


def coverage_gaps(sections: list[str], plan: "UnitPlan", *, min_overlap: float = 0.5) -> list[str]:
    """Outline sections the plan appears to skip — a report of likely omissions,
    not a hard gate.

    Matching is deliberately lenient because strict text matching against
    model-generated topics is brittle, and the noisy real headings carry Markdown
    (``**12.1 Foo**``) and numbering. Each section's *leaf* topic is reduced to its
    content words (emphasis/numbering/stop-words removed) and counted as covered
    when at least ``min_overlap`` of them appear anywhere in the plan's text. A
    heading with no content words of its own (a bare "12.6" whose title lives one
    level up) is skipped rather than falsely flagged. A false "covered" is
    preferred to a false "missing" — this is a safety net behind the prompt's
    coverage contract, not the enforcer."""
    hay: set[str] = set()
    for t in [plan.big_idea, *plan.unit_outcomes] + \
             [d.topic for d in plan.days] + \
             [s for d in plan.days for s in d.objective_seeds]:
        hay |= _match_tokens(t)
    gaps: list[str] = []
    for s in sections:
        want = _match_tokens(s.split(_BREADCRUMB_SEP)[-1])
        if not want:
            continue  # nothing distinctive to match on → don't cry wolf
        if len(want & hay) / len(want) < min_overlap:
            gaps.append(_clean_heading(s))
    return gaps


_VERIFY_SYSTEM = (
    "You judge whether a passage is relevant to a lesson topic. Answer with "
    "exactly one word: 'yes' if the passage would help teach the topic, 'no' if "
    "it is off-topic. Output only 'yes' or 'no'."
)


class GroundingRetriever:
    def __init__(
        self,
        retriever: Retriever,
        config: GroundingConfig | None = None,
        *,
        llm: LLMClient | None = None,
    ) -> None:
        self.retriever = retriever
        self.config = config or GroundingConfig()
        # Used only for the optional LLM relevance check (config.verify). Off by
        # default; the fast model is wired here by the container when enabled.
        self.llm = llm

    def _filter(self, meta: dict[str, Any]) -> dict[str, Any] | None:
        where = {f: meta[f] for f in self.config.filter_fields if meta.get(f) is not None}
        return where or None

    def _passes_floor(self, chunks: list[RetrievedChunk]) -> bool:
        """Top-anchored garbage gate: usable only if the BEST hit clears the floor.
        Recall-biased — it never prunes individual marginal hits, so a correct-
        but-low-scoring chunk survives as long as the top hit is decent."""
        floor = self.config.min_score
        if floor is None or not chunks:
            return bool(chunks)
        return chunks[0].score >= floor

    def _verify(self, query: str, chunks: list[RetrievedChunk]) -> bool:
        """The real judge for high-stakes hits: ask the fast model whether the top
        passage actually helps the topic. Rescues a low-scoring-but-relevant chunk
        and catches a high-scoring-but-off-topic one. Any failure keeps the chunks
        (fail-open — verification must never silently starve generation)."""
        if not self.config.verify or self.llm is None or not chunks:
            return True
        prompt = f"Topic: {query}\n\nPassage:\n{chunks[0].text[:1500]}\n\nRelevant?"
        try:
            answer = self.llm.complete(prompt, system=_VERIFY_SYSTEM, temperature=0.0).text
        except Exception:
            return True
        return not answer.strip().lower().startswith("no")

    def ground(
        self,
        *,
        query: str,
        grade: int | None = None,
        subject: str | None = None,
        framework: str | None = None,
        granularity: Granularity | None = None,
    ) -> GroundingBundle:
        """Retrieve grounding for a brief across the configured collections.

        Metadata filters are applied leniently: if a filtered query returns
        nothing (e.g. the seed corpus lacks that exact grade), it retries the
        same collection unfiltered so grounding degrades to broader context
        rather than to nothing.

        ``framework`` narrows only the collections in
        ``framework_filter_collections`` (exemplars by default), so a
        gradual_release build retrieves gradual_release exemplars instead of
        being anchored on the seeded 5E ones. Framework-agnostic collections
        (curriculum, local_context) are unaffected. The framework filter is a
        *floor*: even the lenient fallback keeps it, so a build never falls back
        to a wrong-framework exemplar — it broadens grade/subject only.

        ``granularity`` (default: config ``default_granularity``) controls how
        much of the book each authoritative hit brings back — narrow chunk,
        whole section, or whole chapter. It applies ONLY to authoritative
        collections (real books with a heading hierarchy); flat corpora
        (curriculum/pedagogical seed) always retrieve narrow, since they have
        nothing to expand into.
        """
        gran: Granularity = granularity or self.config.default_granularity  # type: ignore[assignment]
        authoritative = frozenset(self.config.authoritative_collections)
        meta = {"grade": grade, "subject": subject}
        base_where = self._filter(meta)
        bundle = GroundingBundle(authoritative=authoritative)
        seen: set[str] = set()

        for name in self.config.collections:
            try:
                collection = Collection(name)
            except ValueError:
                continue  # unknown collection name in config → skip, don't crash
            where = base_where
            # The framework filter must survive the lenient fallback: broadening
            # by grade/subject is fine, but a gradual_release build must never
            # fall back to a 5E exemplar. So keep {framework} as the floor filter.
            fallback_where: dict[str, Any] | None = None
            if framework and collection.value in self.config.framework_filter_collections:
                where = {**(base_where or {}), "framework": framework}
                fallback_where = {"framework": framework}
            # only real books (authoritative) are expanded to section/chapter.
            is_auth = collection.value in authoritative
            col_gran: Granularity = gran if is_auth else "narrow"
            chunks = self._retrieve(collection.value, query, where, fallback_where, col_gran)
            # Quality gate: drop the collection's hits when even the best is weak
            # (garbage floor), and, for authoritative books only, when the LLM judge
            # says the top passage is off-topic. Both keep bad context out of the
            # prompt so generation degrades honestly instead of grounding on noise.
            if not self._passes_floor(chunks) or (is_auth and not self._verify(query, chunks)):
                chunks = []
            bundle.chunks[collection.value] = chunks
            for c in chunks:
                src = str(c.payload.get("source", "")).strip()
                if src and src not in seen:
                    seen.add(src)
                    bundle.sources.append(src)
        return bundle

    def skeleton(
        self,
        collection: str,
        *,
        doc_id: str,
        chapter: str | None = None,
    ) -> list[str]:
        """The free, no-LLM table of contents for a book (or one chapter of it):
        the ordered, de-duplicated list of section heading paths.

        This is the cheapest broad-context primitive — enough for a unit planner
        to lay out an arc without pulling (or summarizing) a single page. Returns
        ``[]`` when the source has no heading hierarchy or the store can't fetch."""
        where: dict[str, Any] = {DOC_ID_KEY: doc_id}
        if chapter is not None:
            where[CHAPTER_KEY] = chapter
        try:
            records = self.retriever.vector_store.fetch(collection, where)
        except Exception:
            return []
        records.sort(key=lambda r: r.payload.get(CHUNK_INDEX_KEY, 0))
        headings: list[str] = []
        seen: set[str] = set()
        for r in records:
            hp = str(r.payload.get(HEADING_PATH_KEY, "")).strip()
            if hp and hp not in seen:
                seen.add(hp)
                headings.append(hp)
        return headings

    def outline(
        self,
        *,
        query: str,
        grade: int | None = None,
        subject: str | None = None,
        collection: str | None = None,
    ) -> CoverageOutline:
        """Anchor-then-enumerate: the full section list for the chapter a unit maps
        to.

        A vector query only *locates* the chapter — it does not decide coverage.
        The top ``coverage_anchors`` hits are read (not just the best one) and
        their ``(doc_id, chapter)`` pairs are TALLIED. The chapter the most anchors
        point to wins (so a single mis-ranked hit can't pick the wrong chapter),
        and a chapter that only caught a stray hit is pruned — otherwise a lesson
        on "Wave" would drag in a few "Magnetism" sections that happened to rank.
        A second chapter is kept only when it is co-dominant (``coverage_chapter_
        min_ratio`` of the top chapter's anchor count), for the rare unit that
        genuinely spans two chapters. Each kept chapter's FULL section list is then
        pulled by deterministic metadata scroll (:meth:`skeleton`) — so within the
        chosen chapter no topic is ever dropped.

        ``collection`` defaults to the first authoritative collection (the real
        book). Returns an empty outline — never raises — when nothing anchors or
        the source has no heading hierarchy, so the planner degrades to ungrounded
        arc design instead of crashing."""
        col = collection or next(iter(self.config.authoritative_collections), None)
        if not col:
            return CoverageOutline()
        where = self._filter({"grade": grade, "subject": subject})
        anchors = self._anchor_hits(col, query, where)

        # Tally anchors per (doc_id, chapter), preserving first-seen (best) rank,
        # and remember each book's own chapter_level (stamped at ingest) so the
        # outline is scoped by the book's real hierarchy, not a fixed depth.
        counts: dict[tuple[str, str], int] = {}
        order: list[tuple[str, str]] = []
        levels: dict[str, int] = {}
        for c in anchors:
            doc_id = str(c.payload.get(DOC_ID_KEY, "")).strip()
            chapter = str(c.payload.get(CHAPTER_KEY, "")).strip()
            if not doc_id or not chapter:
                continue  # a flat source with no hierarchy can't be enumerated
            lv = c.payload.get(CHAPTER_LEVEL_KEY)
            if doc_id not in levels and isinstance(lv, int) and lv >= 1:
                levels[doc_id] = lv
            key = (doc_id, chapter)
            if key not in counts:
                counts[key] = 0
                order.append(key)
            counts[key] += 1
        keys = self._dominant_chapters(counts, order)

        out = CoverageOutline()
        seen_sections: set[str] = set()
        for doc_id, chapter in keys:
            sections = self.skeleton(col, doc_id=doc_id, chapter=chapter)
            if not sections:
                continue
            out.chapters.append(_clean_heading(chapter))
            # Collapse each raw heading path to the topic level and de-dup: a deep
            # sub-heading (a callout box) folds into its numbered section, and a
            # section that exists only via sub-headings survives — so the contract
            # lists teachable topics, not every bold line in the book. When the book
            # stamped a chapter_level, scope to it (dropping any unit wrapper above
            # the chapter); otherwise fall back to the fixed coverage_depth from the
            # outermost heading (older ingests with no chapter_level in the payload).
            chapter_level = levels.get(doc_id)
            for raw in sections:
                if chapter_level:
                    label = _clean_heading(_chapter_scoped(raw, chapter_level))
                else:
                    label = _clean_heading(_truncate(raw, self.config.coverage_depth))
                key = label.lower()
                if label and key not in seen_sections:
                    seen_sections.add(key)
                    out.sections.append(label)
        return out

    def _dominant_chapters(
        self, counts: dict[tuple[str, str], int], order: list[tuple[str, str]]
    ) -> list[tuple[str, str]]:
        """The chapter(s) to actually enumerate: the one the most anchors point to,
        plus any co-dominant chapter (>= ``coverage_chapter_min_ratio`` of its anchor
        count AND at least two anchors). This prunes a stray single hit into a
        neighbouring chapter while keeping the plurality winner even when it isn't
        the top-ranked hit."""
        if not order:
            return []
        top = max(counts.values())
        # primary: most anchors, ties broken by best (earliest) rank
        primary = min(order, key=lambda k: (-counts[k], order.index(k)))
        threshold = max(2, math.ceil(top * self.config.coverage_chapter_min_ratio))
        return [k for k in order if k == primary or counts[k] >= threshold]

    def _anchor_hits(
        self, collection: str, query: str, where: dict[str, Any] | None
    ) -> list[RetrievedChunk]:
        """The vector step of :meth:`outline` — top-``coverage_anchors`` hits used
        only to identify chapters. Broadens to unfiltered on an empty filtered
        result (same lenient contract as grounding) and fails open to ``[]``."""
        n = self.config.coverage_anchors
        try:
            hits = self.retriever.retrieve(collection, query, where=where, top_n=n)
            if not hits and where:
                hits = self.retriever.retrieve(collection, query, where=None, top_n=n)
            return hits
        except Exception:
            return []

    def _retrieve(
        self,
        collection: str,
        query: str,
        where: dict[str, Any] | None,
        fallback_where: dict[str, Any] | None = None,
        granularity: Granularity = "narrow",
    ) -> list[RetrievedChunk]:
        """Retrieve with a lenient fallback: if the fully-filtered query returns
        nothing, retry with ``fallback_where`` (the floor filter that must not be
        dropped — e.g. framework) and, failing that, unfiltered."""
        top_n = self.config.per_collection_top_n
        try:
            hits = self.retriever.retrieve(
                collection, query, where=where, top_n=top_n, granularity=granularity
            )
            if not hits and fallback_where and fallback_where != where:
                hits = self.retriever.retrieve(
                    collection, query, where=fallback_where, top_n=top_n, granularity=granularity
                )
            if not hits and where and not fallback_where:  # broaden fully
                hits = self.retriever.retrieve(
                    collection, query, where=None, top_n=top_n, granularity=granularity
                )
            return hits
        except Exception:
            # a missing collection or an unreachable store must not break generation
            return []
