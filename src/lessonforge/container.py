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
from .rag.planner import RetrievalPlanner, build_retrieval_planner
from .rag.retriever import Retriever
from .services.artifact_generation import ArtifactGenerator, build_generator
from .services.artifact_refine import ArtifactRefiner
from .services.chat.context import ContextBuilder
from .services.chat.memory import ConversationMemory
from .services.chat.pipeline import ChatPipeline
from .services.chat.retrieve import ChatRetriever
from .services.chat.store import ChatStore
from .services.chat.synthesize import AnswerSynthesizer
from .services.chat.transform import build_query_transformer
from .services.critique import Critic
from .services.documents.service import DocumentService
from .services.documents.store import DocumentStore
from .services.generation import LessonGenerator
from .services.intake import Intake
from .services.pipeline import LessonPipeline
from .services.profile import ProfileStore
from .services.refine import Refiner
from .services.registry import (
    build_chat_store,
    build_critic,
    build_document_store,
    build_intake,
    build_profile_store,
)
from .services.revise import Reviser
from .services.unit_coherence import UnitCoherence
from .services.unit_generation import UnitGenerator
from .services.unit_planner import UnitPlanner


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
    # Retrieval planner (narrow/section/broad per need). Optional so existing call
    # sites keep working; wired from ``settings.planner`` in ``__post_init__``.
    planner: RetrievalPlanner | None = None
    # M4 stages — optional so existing call sites keep working; ``__post_init__``
    # wires any left unset from ``settings`` (+ the fast model / reasoning model).
    llm_fast: LLMClient | None = None
    intake: Intake | None = None
    critic: Critic | None = None
    reviser: Reviser | None = None
    refiner: Refiner | None = None
    pipeline: LessonPipeline | None = field(default=None)
    # Multi-day unit pipeline (plan-and-expand). Optional; wired in __post_init__
    # from the generator + reviser + retrieval planner already on the container.
    unit_planner: UnitPlanner | None = None
    unit_generator: UnitGenerator | None = None
    profile_store: ProfileStore | None = None
    # The QA chatbot subsystem — persistence + the streaming RAG pipeline. Optional
    # so existing call sites/tests keep working; wired from ``settings.chat`` below.
    chat_store: ChatStore | None = None
    chat_pipeline: ChatPipeline | None = None
    # The document/version memory layer. Built lazily (like chat) so its backend can
    # fail in isolation without 500-ing the lesson/corpus surfaces.
    document_store: DocumentStore | None = None
    document_service: DocumentService | None = None

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
        if self.planner is None:
            self.planner = build_retrieval_planner(self.settings.planner, llm=self.llm_fast)
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
        # multi-day units reuse the same generator + revise loop, day by day.
        if self.unit_planner is None:
            self.unit_planner = UnitPlanner(
                llm=self.llm, grounding=self.grounding, retrieval_planner=self.planner
            )
        if self.unit_generator is None:
            self.unit_generator = UnitGenerator(
                planner=self.unit_planner,
                generator=self.generator,
                reviser=self.reviser,
                coherence=UnitCoherence(),
            )
        # NB: the QA chatbot subsystem (chat_store + chat_pipeline) is built LAZILY,
        # not here — see ``get_chat_store`` / ``get_chat_pipeline``. It talks to its
        # own backend (e.g. Postgres via psycopg) whose failure must degrade *chat*,
        # not the lesson/corpus surfaces that share this container. Building it eagerly
        # would let a chat-store misconfiguration abort the whole container and 500
        # every endpoint (get_container is @lru_cache'd, so the failure would recur).

    # ── QA chatbot: lazily built so its backend can fail in isolation ────────────
    def get_chat_store(self) -> ChatStore:
        """The conversation store, built on first use and cached. Injectable via the
        ``chat_store`` field (tests/fakes); otherwise built from ``settings.chat.store``.
        Kept lazy so a store-backend failure surfaces only on ``/chat`` requests."""
        if self.chat_store is None:
            self.chat_store = build_chat_store(self.settings.chat.store)
        return self.chat_store

    def get_chat_pipeline(self) -> ChatPipeline:
        """The advanced-RAG streaming chat pipeline, built on first use and cached.
        Uses the cheap/fast model for query understanding, compression, and summaries;
        the reasoning model streams the final grounded answer."""
        if self.chat_pipeline is None:
            chat = self.settings.chat
            store = self.get_chat_store()
            self.chat_pipeline = ChatPipeline(
                store=store,
                memory=ConversationMemory(store, chat.memory, llm=self.llm_fast),
                transformer=build_query_transformer(chat.query_transform, llm=self.llm_fast),
                retriever=ChatRetriever(self.retriever, chat.retrieval),
                context_builder=ContextBuilder(chat.context, llm=self.llm_fast),
                synthesizer=AnswerSynthesizer(self.llm, chat.synthesis),
                config=chat,
            )
        return self.chat_pipeline

    # ── document/version memory: lazily built so its backend fails in isolation ──
    def get_document_store(self) -> DocumentStore:
        """The document store, built on first use and cached. Injectable via the
        ``document_store`` field (tests/fakes); otherwise from ``settings.documents``."""
        if self.document_store is None:
            self.document_store = build_document_store(self.settings.documents)
        return self.document_store

    def get_document_service(self) -> DocumentService:
        """The version-graph service over the document store (generate/refine/undo)."""
        if self.document_service is None:
            self.document_service = DocumentService(self.get_document_store())
        return self.document_service

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
            assembly_max_chars=settings.grounding.broad_max_chars,
        )
        # verify uses the fast model (off unless grounding.verify is set).
        grounding = GroundingRetriever(retriever, settings.grounding, llm=llm_fast)
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
