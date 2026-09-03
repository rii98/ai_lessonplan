"""FastEmbed sparse-embedding adapter — the lexical half of hybrid search.

Uses FastEmbed's ``SparseTextEmbedding`` (default ``Qdrant/bm25``): a lightweight,
offline BM25 encoder — no neural model, no GPU, just tokenization + IDF weighting.
The model is loaded lazily so importing the package (and running unit tests) never
triggers a download. Produces the ``{indices, values}`` shape Qdrant expects for a
sparse vector.
"""

from __future__ import annotations

from ...config import SparseEmbeddingConfig
from ..base import SparseEmbedder, SparseVector
from ..registry import register_sparse_embedder


@register_sparse_embedder("fastembed")
class FastEmbedSparseEmbedder(SparseEmbedder):
    def __init__(self, model: str = "Qdrant/bm25") -> None:
        self.model_name = model
        self._model = None  # lazy

    @classmethod
    def from_config(cls, cfg: SparseEmbeddingConfig) -> FastEmbedSparseEmbedder:
        return cls(model=cfg.model)

    def _ensure_model(self):
        if self._model is None:
            from fastembed import SparseTextEmbedding  # imported lazily on purpose

            self._model = SparseTextEmbedding(model_name=self.model_name)
        return self._model

    def embed_sparse(self, texts: list[str]) -> list[SparseVector]:
        model = self._ensure_model()
        return [
            SparseVector(indices=list(map(int, e.indices)), values=list(map(float, e.values)))
            for e in model.embed(texts)
        ]
