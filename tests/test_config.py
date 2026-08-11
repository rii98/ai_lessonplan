"""Config loading, env-var interpolation, and env override."""

from __future__ import annotations

import textwrap

from lessonforge.config import load_settings


def _write(tmp_path, text: str):
    p = tmp_path / "config.yaml"
    p.write_text(textwrap.dedent(text))
    return p


def test_loads_yaml(tmp_path):
    cfg = _write(tmp_path, """
        llm: {provider: ollama, model: gemma4:31b-cloud}
        embedding: {provider: fastembed, model: BAAI/bge-small-en-v1.5}
        reranker: {provider: noop}
        vector_store: {provider: qdrant}
    """)
    s = load_settings(cfg)
    assert s.llm.provider == "ollama"
    assert s.llm.model == "gemma4:31b-cloud"
    assert s.retrieval.top_k == 20  # default applied


def test_env_interpolation_default(tmp_path, monkeypatch):
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    cfg = _write(tmp_path, """
        llm:
          provider: ollama
          model: m
          base_url: ${OLLAMA_BASE_URL:-http://localhost:11434}
        embedding: {provider: fastembed, model: x}
        reranker: {provider: noop}
        vector_store: {provider: qdrant}
    """)
    s = load_settings(cfg)
    assert s.llm.base_url == "http://localhost:11434"


def test_env_interpolation_uses_env(tmp_path, monkeypatch):
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://ollama:11434")
    cfg = _write(tmp_path, """
        llm:
          provider: ollama
          model: m
          base_url: ${OLLAMA_BASE_URL:-http://localhost:11434}
        embedding: {provider: fastembed, model: x}
        reranker: {provider: noop}
        vector_store: {provider: qdrant}
    """)
    s = load_settings(cfg)
    assert s.llm.base_url == "http://ollama:11434"


def test_env_override_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("LF__LLM__MODEL", "overridden-model")
    cfg = _write(tmp_path, """
        llm: {provider: ollama, model: gemma4:31b-cloud}
        embedding: {provider: fastembed, model: x}
        reranker: {provider: noop}
        vector_store: {provider: qdrant}
    """)
    s = load_settings(cfg)
    assert s.llm.model == "overridden-model"


def test_shipped_config_is_valid():
    """The real config/config.yaml must load cleanly."""
    s = load_settings()
    assert s.llm.provider
    assert s.embedding.provider
    assert s.vector_store.provider
