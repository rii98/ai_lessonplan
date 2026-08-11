"""Importing this package registers all built-in adapters.

Each adapter module calls a ``@register_*`` decorator at import time, so simply
importing ``lessonforge.providers`` populates the registries. New backends are
added by dropping a module here and importing it below.
"""

from __future__ import annotations

from .base import (
    Embedder,
    LLMClient,
    LLMResult,
    Reranker,
    RerankResult,
    ScoredRecord,
    VectorRecord,
    VectorStore,
)

# Embedders
from .embedding import fastembed as _fe_embed  # noqa: F401

# LLMs
from .llm import ollama as _ollama  # noqa: F401
from .registry import (
    build_embedder,
    build_llm,
    build_reranker,
    build_vector_store,
)

# Rerankers
from .reranking import fastembed as _fe_rerank  # noqa: F401
from .reranking import http as _http_rerank  # noqa: F401
from .reranking import noop as _noop_rerank  # noqa: F401

# Vector stores
from .vectorstore import qdrant as _qdrant  # noqa: F401

__all__ = [
    "Embedder",
    "LLMClient",
    "LLMResult",
    "RerankResult",
    "Reranker",
    "ScoredRecord",
    "VectorRecord",
    "VectorStore",
    "build_embedder",
    "build_llm",
    "build_reranker",
    "build_vector_store",
]
