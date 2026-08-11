"""No-op reranker — preserves vector-search order. Useful for tests/dev."""

from __future__ import annotations

from ...config import RerankerConfig
from ..base import Reranker, RerankResult
from ..registry import register_reranker


@register_reranker("noop")
class NoopReranker(Reranker):
    @classmethod
    def from_config(cls, cfg: RerankerConfig) -> NoopReranker:
        return cls()

    def rerank(self, query: str, documents: list[str], top_n: int) -> list[RerankResult]:
        return [
            RerankResult(index=i, score=1.0 - i * 1e-3, document=d)
            for i, d in enumerate(documents[:top_n])
        ]
