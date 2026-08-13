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
from .documents import Collection
from .retriever import RetrievedChunk, Retriever


@dataclass(slots=True)
class GroundingBundle:
    chunks: dict[str, list[RetrievedChunk]] = field(default_factory=dict)
    sources: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not any(self.chunks.values())

    def as_prompt_context(self) -> str:
        """Render the bundle as labeled context for the enrichment prompt."""
        if self.is_empty:
            return "No retrieved context available; rely on curriculum knowledge."
        sections: list[str] = [
            "Grounding context (use it; cite these sources in quality.grounding_sources):"
        ]
        for name, chunks in self.chunks.items():
            if not chunks:
                continue
            sections.append(f"\n[{name}]")
            sections.extend(f"- {c.text}  (source: {c.payload.get('source', '?')})"
                            for c in chunks)
        return "\n".join(sections)


class GroundingRetriever:
    def __init__(self, retriever: Retriever, config: GroundingConfig | None = None) -> None:
        self.retriever = retriever
        self.config = config or GroundingConfig()

    def _filter(self, meta: dict[str, Any]) -> dict[str, Any] | None:
        where = {f: meta[f] for f in self.config.filter_fields if meta.get(f) is not None}
        return where or None

    def ground(
        self,
        *,
        query: str,
        grade: int | None = None,
        subject: str | None = None,
        framework: str | None = None,
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
        """
        meta = {"grade": grade, "subject": subject}
        base_where = self._filter(meta)
        bundle = GroundingBundle()
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
            chunks = self._retrieve(collection.value, query, where, fallback_where)
            bundle.chunks[collection.value] = chunks
            for c in chunks:
                src = str(c.payload.get("source", "")).strip()
                if src and src not in seen:
                    seen.add(src)
                    bundle.sources.append(src)
        return bundle

    def _retrieve(
        self,
        collection: str,
        query: str,
        where: dict[str, Any] | None,
        fallback_where: dict[str, Any] | None = None,
    ) -> list[RetrievedChunk]:
        """Retrieve with a lenient fallback: if the fully-filtered query returns
        nothing, retry with ``fallback_where`` (the floor filter that must not be
        dropped — e.g. framework) and, failing that, unfiltered."""
        top_n = self.config.per_collection_top_n
        try:
            hits = self.retriever.retrieve(collection, query, where=where, top_n=top_n)
            if not hits and fallback_where and fallback_where != where:
                hits = self.retriever.retrieve(collection, query, where=fallback_where, top_n=top_n)
            if not hits and where and not fallback_where:  # broaden fully
                hits = self.retriever.retrieve(collection, query, where=None, top_n=top_n)
            return hits
        except Exception:
            # a missing collection or an unreachable store must not break generation
            return []
