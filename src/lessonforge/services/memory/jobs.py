"""MemoryJobQueue — run memory upkeep (summary folds, memory extraction, mastery
commits) OFF the request path, so a slow or failing fold never delays or breaks a turn.

- ``inline`` runs the job immediately (tests, scripts): deterministic.
- ``threadpool`` (default) runs jobs on ONE background worker per process. One worker
  keeps jobs for a conversation in submission order — the rolling summary's
  incremental fold depends on that. A multi-replica deployment that needs strict
  ordering across processes swaps in a Postgres-outbox queue behind this same port.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any

from ... import request_context

log = logging.getLogger("lessonforge.memory.jobs")


class MemoryJobQueue(ABC):
    @abstractmethod
    def submit(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        """Run ``fn`` eventually. Never raises; failures are logged."""


def _safe(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
    try:
        fn(*args, **kwargs)
    except Exception:
        log.exception("memory job %s failed", getattr(fn, "__qualname__", fn))


class InlineJobs(MemoryJobQueue):
    def submit(self, fn, *args, **kwargs):
        _safe(fn, *args, **kwargs)


class ThreadPoolJobs(MemoryJobQueue):
    def __init__(self) -> None:
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="memory")

    def submit(self, fn, *args, **kwargs):
        try:
            request_context.submit_in_context(self._pool, partial(_safe, fn, *args, **kwargs))
        except Exception:   # pool shut down (process exiting): do it now rather than lose it
            _safe(fn, *args, **kwargs)


def build_jobs(provider: str) -> MemoryJobQueue:
    known: dict[str, Callable[[], MemoryJobQueue]] = {"inline": InlineJobs, "threadpool": ThreadPoolJobs}
    try:
        return known[provider]()
    except KeyError:
        raise ValueError(f"unknown memory job queue {provider!r}; known: {sorted(known)}") from None
