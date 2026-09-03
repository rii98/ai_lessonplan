"""ContextBuilder — turn ranked chunks into a numbered, budgeted grounding block.

Sits between retrieval and synthesis. Responsibilities kept deliberately small:

- optional **contextual compression** — an LLM pass that keeps only the
  query-relevant sentences of each chunk, so the budget buys more signal
  (config-gated; off by default);
- **budget packing** — include chunks in rank order until ``max_chars`` is hit,
  so a long context never blows the model's window;
- **numbering** — assign the ``[n]`` markers the synthesizer cites and the UI
  previews, producing the :class:`Citation` list that travels with the answer.

Numbering happens *after* budgeting so the citation list matches exactly what the
model was shown — no dangling ``[n]`` pointing at a dropped chunk.
"""

from __future__ import annotations

from dataclasses import dataclass

from ...config import ChatContextConfig
from ...domain.chat import Citation
from ...providers.base import LLMClient
from .retrieve import ScoredChunk


@dataclass(slots=True)
class BuiltContext:
    text: str                    # the numbered grounding block for the prompt
    citations: list[Citation]    # 1:1 with the [n] markers in ``text``

    @property
    def is_empty(self) -> bool:
        return not self.citations


_COMPRESS_SYSTEM = (
    "Extract ONLY the sentences from the passage that help answer the question. "
    "Keep them verbatim, in order. If nothing is relevant, output NOTHING."
)


class ContextBuilder:
    def __init__(
        self, config: ChatContextConfig, *, llm: LLMClient | None = None
    ) -> None:
        self.config = config
        self.llm = llm

    def build(self, query: str, chunks: list[ScoredChunk]) -> BuiltContext:
        if not chunks:
            return BuiltContext(text="", citations=[])
        if self.config.compression and self.llm is not None:
            chunks = self._compress(query, chunks)

        citations: list[Citation] = []
        blocks: list[str] = []
        used = 0
        for chunk in chunks:
            n = len(citations) + 1
            block = self._render(n, chunk)
            # always include the first chunk even if it alone exceeds the budget,
            # so a big top hit still grounds the answer.
            if used and used + len(block) > self.config.max_chars:
                break
            used += len(block)
            citations.append(
                Citation(
                    n=n, source=chunk.source, collection=chunk.collection,
                    text=chunk.text, score=chunk.score, metadata=chunk.metadata,
                )
            )
            blocks.append(block)
        return BuiltContext(text="\n\n".join(blocks), citations=citations)

    @staticmethod
    def _render(n: int, chunk: ScoredChunk) -> str:
        return f"[{n}] (collection: {chunk.collection}; source: {chunk.source})\n{chunk.text}"

    def _compress(self, query: str, chunks: list[ScoredChunk]) -> list[ScoredChunk]:
        out: list[ScoredChunk] = []
        for chunk in chunks:
            prompt = f"Question: {query}\n\nPassage:\n{chunk.text}\n\nRelevant sentences:"
            try:
                kept = self.llm.complete(prompt, system=_COMPRESS_SYSTEM, temperature=0.0).text.strip()
            except Exception:
                kept = ""
            # keep the original when compression drops everything — never lose a
            # ranked hit to an over-eager filter.
            text = kept or chunk.text
            out.append(
                ScoredChunk(
                    text=text, source=chunk.source, collection=chunk.collection,
                    score=chunk.score, metadata=chunk.metadata,
                )
            )
        return out
