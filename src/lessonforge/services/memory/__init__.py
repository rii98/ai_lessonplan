"""The long-term memory layer (docs/LEARN_MODE.md §7) — L3 memory about the learner,
plus the job queue that keeps all memory upkeep off the request path."""

from __future__ import annotations

from ...config import LongTermMemoryConfig
from ...db.pool import Database
from ...providers.base import Embedder, LLMClient
from .extractor import build_extractor
from .jobs import InlineJobs, MemoryJobQueue, ThreadPoolJobs, build_jobs
from .policy import AllowlistPolicy, sensitive
from .service import MemoryService
from .store import MemoryStore, build_memory_store

__all__ = ["InlineJobs", "MemoryJobQueue", "MemoryService", "MemoryStore", "ThreadPoolJobs",
           "build_jobs", "build_memory_service", "sensitive"]


def build_memory_service(cfg: LongTermMemoryConfig, *, llm_fast: LLMClient, jobs: MemoryJobQueue,
                         store_provider: str, db: Database | None = None,
                         embedder: Embedder | None = None) -> MemoryService:
    return MemoryService(
        store=build_memory_store(store_provider, db=db),
        extractor=build_extractor(cfg.extractor, llm=llm_fast),
        policy=AllowlistPolicy(min_confidence=cfg.min_confidence, max_items=cfg.max_items_per_user),
        jobs=jobs, config=cfg, embedder=embedder,
    )
