"""FastEmbed cross-encoder reranker (local, lazy-loaded)."""

from __future__ import annotations

from ...config import RerankerConfig
from ..base import Reranker, RerankResult
from ..registry import register_reranker


@register_reranker("fastembed")
class FastEmbedReranker(Reranker):
    def __init__(self, model: str) -> None:
        self.model_name = model
        self._model = None  # lazy

    @classmethod
    def from_config(cls, cfg: RerankerConfig) -> FastEmbedReranker:
        if not cfg.model:
            raise ValueError("reranker.model is required for the fastembed provider")
        return cls(model=cfg.model)

    def _ensure_model(self):
        if self._model is None:
            from fastembed.rerank.cross_encoder import TextCrossEncoder

            self._model = TextCrossEncoder(model_name=self.model_name)
        return self._model

    def rerank(self, query: str, documents: list[str], top_n: int) -> list[RerankResult]:
        if not documents:
            return []
        model = self._ensure_model()
        scores = list(model.rerank(query, documents))
        ranked = sorted(
            (RerankResult(index=i, score=float(s), document=documents[i])
             for i, s in enumerate(scores)),
            key=lambda r: r.score,
            reverse=True,
        )
        return ranked[:top_n]
