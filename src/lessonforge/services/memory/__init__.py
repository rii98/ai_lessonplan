"""The long-term memory layer (docs/MEMORY.md) — L3 memory about the learner, plus the
job queue that keeps all memory upkeep off the request path."""

from __future__ import annotations

from ...config import LongTermMemoryConfig
from ...db.pool import Database
from ...providers.base import Embedder, LLMClient
from .extractor import build_extractor
from .jobs import InlineJobs, MemoryJobQueue, ThreadPoolJobs, build_jobs
from .policy import AllowlistPolicy, sensitive
from .profile import build_synthesizer
from .reconciler import build_reconciler
from .service import MemoryRefused, MemoryService
from .store import MemoryStore, build_memory_store

__all__ = ["InlineJobs", "MemoryJobQueue", "MemoryRefused", "MemoryService", "MemoryStore",
           "ThreadPoolJobs", "build_jobs", "build_memory_service", "sensitive"]


def build_memory_service(cfg: LongTermMemoryConfig, *, llm_fast: LLMClient, jobs: MemoryJobQueue,
                         store_provider: str, db: Database | None = None,
                         embedder: Embedder | None = None) -> MemoryService:
    """Every stage is a config-swappable port: store · extractor · reconciler · profile."""
    return MemoryService(
        store=build_memory_store(store_provider, db=db),
        extractor=build_extractor(cfg.extractor, llm=llm_fast, max_candidates=cfg.max_candidates),
        policy=AllowlistPolicy(min_confidence=cfg.min_confidence),
        reconciler=build_reconciler(cfg.reconciler, llm=llm_fast,
                                    duplicate_similarity=cfg.duplicate_similarity),
        synthesizer=build_synthesizer(cfg.profile, llm=llm_fast),
        jobs=jobs, config=cfg, embedder=embedder,
    )
