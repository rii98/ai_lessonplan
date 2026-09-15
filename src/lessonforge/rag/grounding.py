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

from dataclasses import dataclass, field
from typing import Any

from ..config import GroundingConfig
from ..providers.base import LLMClient
from .documents import (
    CHAPTER_KEY,
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
