"""FastEmbed embedding adapter.

FastEmbed runs ONNX embedding models locally (no server, no GPU required).
The model is loaded lazily on first use so importing the package — and running
unit tests — never triggers a model download.
"""

from __future__ import annotations

from ...config import EmbeddingConfig
from ..base import Embedder
from ..registry import register_embedder

# Known output dims for common FastEmbed models (avoids an eager load just to
# learn the dimension). Falls back to a probe embed for anything not listed.
_KNOWN_DIMS = {
    "BAAI/bge-small-en-v1.5": 384,
    "BAAI/bge-base-en-v1.5": 768,
    "sentence-transformers/all-MiniLM-L6-v2": 384,
}


@register_embedder("fastembed")
class FastEmbedEmbedder(Embedder):
    def __init__(self, model: str, dim: int | None = None) -> None:
        self.model_name = model
        self._dim = dim if dim is not None else _KNOWN_DIMS.get(model)
        self._model = None  # lazy

    @classmethod
    def from_config(cls, cfg: EmbeddingConfig) -> FastEmbedEmbedder:
        return cls(model=cfg.model, dim=cfg.dim)

    def _ensure_model(self):
        if self._model is None:
            from fastembed import TextEmbedding  # imported lazily on purpose

            self._model = TextEmbedding(model_name=self.model_name)
        return self._model

    @property
    def dim(self) -> int:
        if self._dim is None:
            self._dim = len(self.embed_one("dimension probe"))
        return self._dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        model = self._ensure_model()
        return [vec.tolist() for vec in model.embed(texts)]
