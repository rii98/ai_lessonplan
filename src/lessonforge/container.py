"""Composition root: build every component from Settings, in one place.

Nothing else in the codebase constructs adapters directly — they receive the
interfaces from this container. That keeps wiring centralized and makes tests
trivial: build a Container with fakes, or override individual fields.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import providers  # noqa: F401  (import triggers adapter registration)
from .config import Settings, load_settings
from .export import ExportService
from .providers.base import Embedder, LLMClient, Reranker, VectorStore
from .providers.registry import (
    build_embedder,
    build_llm,
    build_reranker,
    build_vector_store,
)
from .rag.grounding import GroundingRetriever
from .rag.retriever import Retriever
from .services.critique import Critic
from .services.generation import LessonGenerator
from .services.intake import Intake
from .services.pipeline import LessonPipeline
from .services.registry import build_critic, build_intake
from .services.revise import Reviser


@dataclass
class Container:
    settings: Settings
    llm: LLMClient
    embedder: Embedder
    reranker: Reranker
    vector_store: VectorStore
    retriever: Retriever
    grounding: GroundingRetriever
    generator: LessonGenerator
    exporter: ExportService
    # M4 stages — optional so existing call sites keep working; ``__post_init__``
    # wires any left unset from ``settings`` (+ the fast model / reasoning model).
    llm_fast: LLMClient | None = None
    intake: Intake | None = None
    critic: Critic | None = None
    reviser: Reviser | None = None
    pipeline: LessonPipeline | None = field(default=None)

    def __post_init__(self) -> None:
        # intake uses the cheap fast model; critique/revise use the reasoning model.
        self.llm_fast = self.llm_fast or self.llm
        if self.intake is None:
            self.intake = build_intake(self.settings.intake, llm=self.llm_fast)
        if self.critic is None:
            self.critic = build_critic(self.settings.critique, llm=self.llm)
        if self.reviser is None:
            self.reviser = Reviser.from_config(
                self.settings.critique, llm=self.llm, critic=self.critic
            )
        if self.pipeline is None:
            self.pipeline = LessonPipeline(
                intake=self.intake, generator=self.generator, reviser=self.reviser
            )

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> Container:
        settings = settings or load_settings()
        llm = build_llm(settings.llm)
        # per-stage models: a cheap fast model for intake, the reasoning model for
        # everything else. Optional — falls back to ``llm`` when unconfigured.
        llm_fast = build_llm(settings.llm_fast) if settings.llm_fast else llm
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
        grounding = GroundingRetriever(retriever, settings.grounding)
        generator = LessonGenerator(llm=llm, grounding=grounding)
        exporter = ExportService(settings.export)
        return cls(
            settings=settings,
            llm=llm,
            llm_fast=llm_fast,
            embedder=embedder,
            reranker=reranker,
            vector_store=vector_store,
            retriever=retriever,
            grounding=grounding,
            generator=generator,
            exporter=exporter,
        )
