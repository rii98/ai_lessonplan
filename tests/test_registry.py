"""The registry is the mechanism behind 'one config change swaps everything'."""

from __future__ import annotations

import pytest

import lessonforge.providers  # noqa: F401  (registers built-ins)
from lessonforge.config import (
    EmbeddingConfig,
    LLMConfig,
    RerankerConfig,
    VectorStoreConfig,
)
from lessonforge.providers.llm.ollama import OllamaLLM
from lessonforge.providers.registry import (
    build_embedder,
    build_llm,
    build_reranker,
    build_vector_store,
)
from lessonforge.providers.reranking.noop import NoopReranker
from lessonforge.providers.vectorstore.qdrant import QdrantVectorStore


def test_build_llm_selects_class_by_provider():
    llm = build_llm(LLMConfig(provider="ollama", model="gemma4:31b-cloud"))
    assert isinstance(llm, OllamaLLM)
    assert llm.model == "gemma4:31b-cloud"


def test_build_reranker_swap_is_one_field():
    r = build_reranker(RerankerConfig(provider="noop"))
    assert isinstance(r, NoopReranker)


def test_build_vector_store():
    vs = build_vector_store(VectorStoreConfig(provider="qdrant"))
    assert isinstance(vs, QdrantVectorStore)


def test_build_embedder_does_not_load_model():
    # constructing must not download or load the ONNX model (lazy)
    emb = build_embedder(EmbeddingConfig(provider="fastembed", model="BAAI/bge-small-en-v1.5"))
    assert emb.dim == 384  # from the known-dims table, no model load


def test_unknown_provider_fails_loudly():
    with pytest.raises(ValueError) as exc:
        build_llm(LLMConfig(provider="does-not-exist", model="m"))
    assert "Unknown llm provider" in str(exc.value)
    assert "Available" in str(exc.value)
