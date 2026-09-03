"""QueryTransformer — the advanced query-understanding stage.

Turns a raw user turn (plus the conversation so far) into the set of queries the
retriever should actually run. Three independently-gated techniques, all
config-driven (:class:`ChatQueryTransformConfig`):

- **History-aware condensation** — rewrite a follow-up ("what about grade 7?")
  into a standalone question, so retrieval doesn't depend on chat context it
  can't see.
- **Multi-query expansion** — generate N paraphrase / sub-question variants and
  retrieve for each; fusing their results recovers documents any single phrasing
  would miss.
- **HyDE** — generate a hypothetical answer and retrieve with *it*; a full
  passage often embeds closer to the real source than a terse question does.

A ``passthrough`` provider (no LLM) is the deterministic seam for tests and
offline runs. Providers self-register; the transformer is resolved by config —
same loose-coupling contract as the rest of the system.
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field

from ...config import ChatQueryTransformConfig
from ...domain.chat import ChatMessage, Role
from ...providers.base import LLMClient


@dataclass(slots=True)
class TransformedQuery:
    """The queries to retrieve with, plus what produced them (for diagnostics)."""

    queries: list[str]
    standalone: str          # the condensed standalone question (also queries[0])
    expansions: list[str] = field(default_factory=list)
    hyde_doc: str | None = None

    def meta(self) -> dict[str, object]:
        return {
            "standalone": self.standalone,
            "expansions": self.expansions,
            "hyde": bool(self.hyde_doc),
            "query_count": len(self.queries),
        }


class QueryTransformer(ABC):
    @abstractmethod
    def transform(self, question: str, history: list[ChatMessage]) -> TransformedQuery:
        """Return the retrieval queries for this turn given prior messages."""

    @classmethod
    def from_config(
        cls, cfg: ChatQueryTransformConfig, *, llm: LLMClient
    ) -> QueryTransformer:  # pragma: no cover
        raise NotImplementedError


_REGISTRY: dict[str, type[QueryTransformer]] = {}


def register_query_transformer(name: str) -> Callable[[type[QueryTransformer]], type[QueryTransformer]]:
    def deco(cls: type[QueryTransformer]) -> type[QueryTransformer]:
        _REGISTRY[name] = cls
        return cls

    return deco


def build_query_transformer(
    cfg: ChatQueryTransformConfig, *, llm: LLMClient
) -> QueryTransformer:
    try:
        cls = _REGISTRY[cfg.provider]
    except KeyError:
        available = ", ".join(sorted(_REGISTRY)) or "<none>"
        raise ValueError(
            f"Unknown query_transform provider {cfg.provider!r}. Available: {available}."
        ) from None
    return cls.from_config(cfg, llm=llm)


@register_query_transformer("passthrough")
class PassthroughTransformer(QueryTransformer):
    """No transformation: retrieve with the raw question. Deterministic, free."""

    @classmethod
    def from_config(cls, cfg, *, llm) -> PassthroughTransformer:
        return cls()

    def transform(self, question: str, history: list[ChatMessage]) -> TransformedQuery:
        return TransformedQuery(queries=[question], standalone=question)


_CONDENSE_SYSTEM = (
    "You rewrite a follow-up question into a standalone question using the "
    "conversation so far. Resolve pronouns and implicit references. Output ONLY "
    "the rewritten question, nothing else. If it is already standalone, return it "
    "unchanged."
)
_EXPAND_SYSTEM = (
    "You expand a search query into distinct alternative phrasings and "
    "sub-questions that would retrieve relevant passages from a curriculum "
    "knowledge base. Output ONLY a JSON array of short query strings."
)
_HYDE_SYSTEM = (
    "You write a brief, plausible textbook-style passage that would answer the "
    "question, as if quoted from curriculum material. 2-4 sentences. Output only "
    "the passage."
)


@register_query_transformer("llm")
class LLMTransformer(QueryTransformer):
    """LLM-powered condensation + expansion + HyDE, each gated by config. Uses the
    fast model. Any step that errors or returns garbage degrades to skipping that
    step — retrieval always gets at least the (condensed) question."""

    def __init__(self, llm: LLMClient, cfg: ChatQueryTransformConfig) -> None:
        self.llm = llm
        self.cfg = cfg

    @classmethod
    def from_config(cls, cfg, *, llm) -> LLMTransformer:
        return cls(llm, cfg)

    def transform(self, question: str, history: list[ChatMessage]) -> TransformedQuery:
        standalone = self._condense(question, history) if self.cfg.condense_history else question
        queries: list[str] = [standalone]
        expansions: list[str] = []
        if self.cfg.expansions > 0:
            expansions = self._expand(standalone, self.cfg.expansions)
            queries.extend(expansions)
        hyde_doc: str | None = None
        if self.cfg.hyde:
            hyde_doc = self._hyde(standalone)
            if hyde_doc:
                queries.append(hyde_doc)
        # de-dup while preserving order (a no-op expansion shouldn't retrieve twice)
        seen: dict[str, None] = {}
        for q in queries:
            q = q.strip()
            if q:
                seen.setdefault(q, None)
        return TransformedQuery(
            queries=list(seen) or [question],
            standalone=standalone,
            expansions=expansions,
            hyde_doc=hyde_doc,
        )

    def _condense(self, question: str, history: list[ChatMessage]) -> str:
        if not history:
            return question
        convo = "\n".join(
            f"{m.role.value.capitalize()}: {m.content}"
            for m in history
            if m.role in (Role.user, Role.assistant)
        )
        prompt = f"Conversation so far:\n{convo}\n\nFollow-up question: {question}\n\nStandalone question:"
        try:
            text = self.llm.complete(prompt, system=_CONDENSE_SYSTEM, temperature=0.0).text
            text = text.strip().strip('"')
            return text or question
        except Exception:
            return question

    def _expand(self, query: str, n: int) -> list[str]:
        prompt = f"Query: {query}\n\nReturn {n} alternative queries as a JSON array."
        try:
            text = self.llm.complete(prompt, system=_EXPAND_SYSTEM, temperature=0.3).text
            items = _parse_json_list(text)
            return [str(x).strip() for x in items if str(x).strip()][:n]
        except Exception:
            return []

    def _hyde(self, query: str) -> str | None:
        try:
            text = self.llm.complete(query, system=_HYDE_SYSTEM, temperature=0.3).text
            return text.strip() or None
        except Exception:
            return None


def _parse_json_list(text: str) -> list[object]:
    """Lenient JSON-array parse: cloud models often wrap the array in prose or a
    ```json fence. Extract the first bracketed array and parse it."""
    text = text.strip()
    try:
        val = json.loads(text)
        if isinstance(val, list):
            return val
    except json.JSONDecodeError:
        pass
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if m:
        try:
            val = json.loads(m.group(0))
            if isinstance(val, list):
                return val
        except json.JSONDecodeError:
            pass
    return []
