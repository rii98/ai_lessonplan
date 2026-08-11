"""Composition root: build every component from Settings, in one place.

Nothing else in the codebase constructs adapters directly — they receive the
interfaces from this container. That keeps wiring centralized and makes tests
trivial: build a Container with fakes, or override individual fields.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import providers  # noqa: F401  (import triggers adapter registration)
from .config import Settings, load_settings
from .providers.base import Embedder, LLMClient, Reranker, VectorStore
from .providers.registry import (
    build_embedder,
    build_llm,
    build_reranker,
    build_vector_store,
)
from .rag.retriever import Retriever
from .services.generation import LessonGenerator


@dataclass
class Container:
    settings: Settings
    llm: LLMClient
    embedder: Embedder
    reranker: Reranker
    vector_store: VectorStore
    retriever: Retriever
    generator: LessonGenerator

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> Container:
        settings = settings or load_settings()
        llm = build_llm(settings.llm)
        embedder = build_embedder(settings.embedding)
        reranker = build_reranker(settings.reranker)
        vector_store = build_vector_store(settings.vector_store)
        retriever = Retriever(
            embedder=embedder,
            vector_store=vector_store,
            reranker=reranker,
            top_k=settings.retrieval.top_k,
            rerank_top_n=settings.retrieval.rerank_top_n,
        )
        generator = LessonGenerator(llm=llm, retriever=retriever)
        return cls(
            settings=settings,
            llm=llm,
            embedder=embedder,
            reranker=reranker,
            vector_store=vector_store,
            retriever=retriever,
            generator=generator,
        )
