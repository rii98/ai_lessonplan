"""ContextService — cached, out-of-band-refreshed user context.

- Reads return the context stored on the principal (cached in-process briefly);
  when it is older than ``context.ttl_h`` a refresh is scheduled in the background.
  **Requests never wait on Edboox.**
- Fails soft: if the upstream is down the last-known context is kept; with none
  stored yet, the token claims are the fallback.
- One refresh per principal in flight at a time (no stampede on a cold cache).
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import Executor, ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from ..auth.service import TTLCache
from .model import UserContext
from .provider import ContextUnavailable, UserContextProvider, claims_context

if TYPE_CHECKING:
    from ..auth.directory import PrincipalDirectory
    from ..auth.principal import Principal

log = logging.getLogger(__name__)


class ContextService:
    def __init__(self, provider: UserContextProvider, directory: PrincipalDirectory, *,
                 ttl_h: float, cache_s: float = 60.0, executor: Executor | None = None) -> None:
        self.provider = provider
        self.directory = directory
        self.ttl = timedelta(hours=ttl_h)
        self._cache: TTLCache[str, UserContext] = TTLCache(cache_s)
        self._executor = executor or ThreadPoolExecutor(max_workers=2,
                                                        thread_name_prefix="ctx-refresh")
        self._inflight: set[str] = set()
        self._lock = threading.Lock()

    def context_for(self, principal: Principal) -> UserContext:
        cached = self._cache.get(principal.id)
        if cached is not None:
            return cached
        rec = self.directory.get(principal.id)
        ctx: UserContext | None = None
        if rec is not None and rec.context:
            try:
                ctx = UserContext.model_validate(rec.context)
            except Exception:   # an old/corrupt blob is just a cache miss
                ctx = None
        if ctx is None or ctx.synced_at is None or _age(ctx) > self.ttl:
            self.refresh_async(principal)
        ctx = ctx or claims_context(principal)
        self._cache.put(principal.id, ctx)
        return ctx

    def refresh_async(self, principal: Principal) -> None:
        with self._lock:
            if principal.id in self._inflight:
                return
            self._inflight.add(principal.id)
        self._executor.submit(self._refresh_logged, principal)

    def refresh(self, principal: Principal) -> UserContext | None:
        """Fetch now and store (admin CLI ``sync-context``; tests). Returns the new
        context, or ``None`` when upstream has no record or is unavailable."""
        try:
            ctx = self.provider.fetch(principal)
        except ContextUnavailable as exc:
            log.warning("context refresh for %s kept last-known: %s", principal.subject, exc)
            return None
        if ctx is None:
            return None
        ctx = ctx.model_copy(update={"synced_at": datetime.now(UTC)})
        self.directory.save_context(principal.id, ctx.model_dump(mode="json"))
        self._cache.put(principal.id, ctx)
        return ctx

    def _refresh_logged(self, principal: Principal) -> None:
        try:
            self.refresh(principal)
        except Exception:
            log.exception("context refresh failed for %s", principal.subject)
        finally:
            with self._lock:
                self._inflight.discard(principal.id)


def _age(ctx: UserContext) -> timedelta:
    synced = ctx.synced_at
    if synced is None:
        return timedelta.max
    if synced.tzinfo is None:
        synced = synced.replace(tzinfo=UTC)
    return datetime.now(UTC) - synced
