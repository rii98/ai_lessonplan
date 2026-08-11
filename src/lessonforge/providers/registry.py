"""Provider registry — maps a config ``provider`` string to an adapter class.

Adapters self-register via decorators::

    @register_llm("ollama")
    class OllamaLLM(LLMClient): ...

The build_* functions read the relevant config block, look up the class by
``provider``, and construct it. An unknown provider raises a clear error that
lists what IS available — so a typo in config fails loudly at startup, never
silently at request time.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from ..config import (
    EmbeddingConfig,
    LLMConfig,
    RerankerConfig,
    VectorStoreConfig,
)
from .base import Embedder, LLMClient, Reranker, VectorStore

T = TypeVar("T")

LLM_REGISTRY: dict[str, type[LLMClient]] = {}
EMBEDDER_REGISTRY: dict[str, type[Embedder]] = {}
RERANKER_REGISTRY: dict[str, type[Reranker]] = {}
VECTOR_STORE_REGISTRY: dict[str, type[VectorStore]] = {}


def _register(registry: dict[str, type[T]], name: str) -> Callable[[type[T]], type[T]]:
    def deco(cls: type[T]) -> type[T]:
        if name in registry:
            raise ValueError(f"provider {name!r} already registered as {registry[name]!r}")
        registry[name] = cls
        return cls

    return deco


def register_llm(name: str):
    return _register(LLM_REGISTRY, name)


def register_embedder(name: str):
    return _register(EMBEDDER_REGISTRY, name)


def register_reranker(name: str):
    return _register(RERANKER_REGISTRY, name)


def register_vector_store(name: str):
    return _register(VECTOR_STORE_REGISTRY, name)


def _lookup(registry: dict[str, type[T]], provider: str, kind: str) -> type[T]:
    try:
        return registry[provider]
    except KeyError:
        available = ", ".join(sorted(registry)) or "<none registered>"
        raise ValueError(
            f"Unknown {kind} provider {provider!r}. Available: {available}. "
            f"Check config/config.yaml or register the adapter."
        ) from None


def build_llm(cfg: LLMConfig) -> LLMClient:
    return _lookup(LLM_REGISTRY, cfg.provider, "llm").from_config(cfg)  # type: ignore[attr-defined]


def build_embedder(cfg: EmbeddingConfig) -> Embedder:
    return _lookup(EMBEDDER_REGISTRY, cfg.provider, "embedding").from_config(cfg)  # type: ignore[attr-defined]


def build_reranker(cfg: RerankerConfig) -> Reranker:
    return _lookup(RERANKER_REGISTRY, cfg.provider, "reranker").from_config(cfg)  # type: ignore[attr-defined]


def build_vector_store(cfg: VectorStoreConfig) -> VectorStore:
    return _lookup(VECTOR_STORE_REGISTRY, cfg.provider, "vector_store").from_config(cfg)  # type: ignore[attr-defined]
