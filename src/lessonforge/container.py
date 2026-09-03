"""Composition root: build every component from Settings, in one place.

Nothing else in the codebase constructs adapters directly — they receive the
interfaces from this container. That keeps wiring centralized and makes tests
trivial: build a Container with fakes, or override individual fields.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import providers  # noqa: F401  (import triggers adapter registration)
from .config import Settings, SparseEmbeddingConfig, load_settings
from .export import ExportService
from .export.base import ArtifactKind
from .providers.base import Embedder, LLMClient, Reranker, SparseEmbedder, VectorStore
from .providers.registry import (
    build_embedder,
    build_llm,
    build_reranker,
    build_sparse_embedder,
    build_vector_store,
)
from .rag.grounding import GroundingRetriever
from .rag.ingest import ChunkerPolicy, Ingestor
from .rag.retriever import Retriever
from .services.artifact_generation import ArtifactGenerator, build_generator
from .services.artifact_refine import ArtifactRefiner
from .services.critique import Critic
from .services.generation import LessonGenerator
from .services.intake import Intake
from .services.pipeline import LessonPipeline
from .services.profile import ProfileStore
from .services.refine import Refiner
from .services.registry import build_critic, build_intake, build_profile_store
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
    sparse_embedder: SparseEmbedder | None = None  # hybrid search; None = dense-only
    ingestor: Ingestor | None = None
    # M4 stages — optional so existing call sites keep working; ``__post_init__``
    # wires any left unset from ``settings`` (+ the fast model / reasoning model).
    llm_fast: LLMClient | None = None
    intake: Intake | None = None
    critic: Critic | None = None
    reviser: Reviser | None = None
    refiner: Refiner | None = None
    pipeline: LessonPipeline | None = field(default=None)
    profile_store: ProfileStore | None = None

    def __post_init__(self) -> None:
        # intake uses the cheap fast model; critique/revise use the reasoning model.
        self.llm_fast = self.llm_fast or self.llm
        if self.ingestor is None:
            self.ingestor = Ingestor(
                embedder=self.embedder,
                vector_store=self.vector_store,
                chunker_policy=ChunkerPolicy.from_config(self.settings.chunking),
                sparse_embedder=self.sparse_embedder,
                hybrid=self.settings.retrieval.hybrid,
            )
        if self.profile_store is None:
            self.profile_store = build_profile_store(self.settings.profile)
        if self.intake is None:
            self.intake = build_intake(self.settings.intake, llm=self.llm_fast)
        if self.critic is None:
            self.critic = build_critic(self.settings.critique, llm=self.llm)
        if self.reviser is None:
            self.reviser = Reviser.from_config(
                self.settings.critique, llm=self.llm, critic=self.critic
            )
        if self.refiner is None:
            self.refiner = Refiner.from_config(
                self.settings.refine, llm=self.llm, critic=self.critic,
                grounding=self.grounding,
            )
        if self.pipeline is None:
            self.pipeline = LessonPipeline(
                intake=self.intake, generator=self.generator, reviser=self.reviser
            )

    def artifact_generator(self, kind: ArtifactKind) -> ArtifactGenerator:
        """Build a targeted-artifact generator (quiz/worksheet/slides/…) for
        ``kind``, wired with the reasoning LLM, the shared grounding retriever (so
        it uses the ``reference`` book), and the configured repair budget. Built
        per request — generators are cheap and hold no state."""
        return build_generator(
            kind,
            llm=self.llm,
            grounding=self.grounding,
            max_repairs=self.settings.generation.max_repairs,
        )

    def artifact_refiner(self, kind: ArtifactKind) -> ArtifactRefiner:
        """Build the AI "Improve" refiner for a targeted artifact — wired with the
        reasoning LLM and the shared (hybrid) grounding retriever, so a content edit
        re-grounds in the reference book. Per request; holds no state."""
        from .domain.artifact_sections import ARTIFACT_MODELS, ARTIFACT_SECTIONS

        k = kind.value
        return ArtifactRefiner(
            kind=k,
            model=ARTIFACT_MODELS[k],
            sections=ARTIFACT_SECTIONS[k],
            llm=self.llm,
            grounding=self.grounding,
            max_cascade=self.settings.refine.max_cascade,
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
        # the lexical encoder for hybrid search — built only when hybrid is on, with
        # a BM25 default when no sparse_embedding block is configured.
        sparse_embedder = (
            build_sparse_embedder(settings.sparse_embedding or SparseEmbeddingConfig(provider="fastembed"))
            if settings.retrieval.hybrid else None
        )
        retriever = Retriever(
            embedder=embedder,
            vector_store=vector_store,
            reranker=reranker,
            sparse_embedder=sparse_embedder,
            hybrid=settings.retrieval.hybrid,
            top_k=settings.retrieval.top_k,
            rerank_top_n=settings.retrieval.rerank_top_n,
        )
        grounding = GroundingRetriever(retriever, settings.grounding)
        generator = LessonGenerator(
            llm=llm, grounding=grounding, max_repairs=settings.generation.max_repairs
        )
        exporter = ExportService(settings.export)
        return cls(
            settings=settings,
            llm=llm,
            llm_fast=llm_fast,
            embedder=embedder,
            sparse_embedder=sparse_embedder,
            reranker=reranker,
            vector_store=vector_store,
            retriever=retriever,
            grounding=grounding,
            generator=generator,
            exporter=exporter,
        )
