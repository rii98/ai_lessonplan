"""Platform — composition root for the cross-cutting services the AI pipeline
runs inside: the database pool, identity & access, knowledge spaces, user
context, activity and audit.

Kept separate from :class:`~lessonforge.container.Container` (which composes the
lesson/chat pipeline) so each composition root has one reason to change. Every
component is built lazily on first use and cached; tests inject fakes by
assigning the attribute before first use. :meth:`validate` performs the boot-time
checks that must fail the deploy rather than the first request.
"""

from __future__ import annotations

import logging
from functools import cached_property
from typing import Any

from .activity.store import (
    ActivityReader,
    ActivityRecorder,
    BufferedRecorder,
    MemoryActivityStore,
    NoopRecorder,
    PostgresActivityStore,
)
from .audit import AuditLog, MemoryAuditLog, PostgresAuditLog
from .auth.directory import MemoryPrincipalDirectory, PostgresPrincipalDirectory, PrincipalDirectory
from .auth.policy import RbacPolicy
from .auth.replay import MemoryReplayGuard, PostgresReplayGuard, ReplayGuard
from .auth.service import AuthService, SessionCodec
from .auth.verifier import JwtVerifier, TokenVerifier
from .config import Settings
from .context.provider import build_context_provider
from .context.scope_policy import ScopePolicy
from .context.service import ContextService
from .db.pool import Database
from .domain.vocabulary import Vocabulary, load_vocabulary
from .knowledge.registry import (
    KnowledgeRegistry,
    MemoryKnowledgeRegistry,
    PostgresKnowledgeRegistry,
)
from .knowledge.scope import KnowledgePolicy

log = logging.getLogger(__name__)

_PG = "postgres"


def _choose(provider: str, kind: str, memory: Any, postgres: Any) -> Any:
    if provider == "memory":
        return memory()
    if provider == _PG:
        return postgres()
    raise ValueError(f"Unknown {kind} provider {provider!r}. Available: memory, postgres")


class Platform:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    # ── persistence ──────────────────────────────────────────────────────────
    @property
    def uses_postgres(self) -> bool:
        s = self.settings
        return _PG in {s.database.provider, s.chat.store.provider, s.documents.provider,
                       s.profile.provider, s.activity.provider}

    @cached_property
    def db(self) -> Database | None:
        return Database(self.settings.database) if self.uses_postgres else None

    def _require_db(self) -> Database:
        if self.db is None:  # pragma: no cover - guarded by uses_postgres
            raise ValueError("a postgres provider is configured but database.url is empty")
        return self.db

    # ── identity & access ────────────────────────────────────────────────────
    @cached_property
    def rbac(self) -> RbacPolicy:
        return RbacPolicy.from_config(self.settings.rbac, self.settings.auth)

    @cached_property
    def directory(self) -> PrincipalDirectory:
        return _choose(self.settings.database.provider, "database", MemoryPrincipalDirectory,
                       lambda: PostgresPrincipalDirectory(self._require_db()))

    @cached_property
    def replay(self) -> ReplayGuard:
        return _choose(self.settings.database.provider, "database", MemoryReplayGuard,
                       lambda: PostgresReplayGuard(self._require_db()))

    @cached_property
    def audit(self) -> AuditLog:
        return _choose(self.settings.database.provider, "database", MemoryAuditLog,
                       lambda: PostgresAuditLog(self._require_db()))

    @cached_property
    def verifier(self) -> TokenVerifier:
        return JwtVerifier(self.settings.auth)

    @cached_property
    def auth(self) -> AuthService:
        cfg = self.settings.auth
        if not cfg.issuers:
            raise ValueError("auth.issuers is empty — no token issuer is trusted")
        return AuthService(cfg, verifier=self.verifier, directory=self.directory,
                           replay=self.replay, policy=self.rbac,
                           sessions=SessionCodec(cfg.session.secret, cfg.session.ttl_min))

    # ── knowledge spaces ─────────────────────────────────────────────────────
    @cached_property
    def knowledge_policy(self) -> KnowledgePolicy:
        return KnowledgePolicy(self.settings.knowledge)

    @cached_property
    def knowledge_registry(self) -> KnowledgeRegistry:
        return _choose(self.settings.database.provider, "database", MemoryKnowledgeRegistry,
                       lambda: PostgresKnowledgeRegistry(self._require_db()))

    # ── user context ─────────────────────────────────────────────────────────
    @cached_property
    def vocabulary(self) -> Vocabulary:
        return load_vocabulary(self.settings.vocabulary_path)

    @cached_property
    def context(self) -> ContextService:
        provider = build_context_provider(self.settings.context, self.vocabulary)
        return ContextService(provider, self.directory, ttl_h=self.settings.context.ttl_h,
                              cache_s=float(self.settings.auth.principal_cache_s or 60))

    @cached_property
    def scope_policy(self) -> ScopePolicy:
        return ScopePolicy(self.settings.scope)

    # ── activity ─────────────────────────────────────────────────────────────
    @cached_property
    def _activity_store(self) -> tuple[ActivityRecorder, ActivityReader]:
        provider = self.settings.activity.provider
        if provider == "memory":
            mem = MemoryActivityStore()
            return mem, mem
        if provider == "noop":
            return NoopRecorder(), MemoryActivityStore()
        if provider == _PG:
            pg = PostgresActivityStore(self._require_db())
            return BufferedRecorder(pg), pg
        raise ValueError(f"Unknown activity provider {provider!r}. Available: postgres, memory, noop")

    @property
    def activity(self) -> ActivityRecorder:
        return self._activity_store[0]

    @property
    def activity_reader(self) -> ActivityReader:
        return self._activity_store[1]

    # ── lifecycle ────────────────────────────────────────────────────────────
    def warm(self, *, require_auth: bool = True) -> None:
        """Build every component now, single-threaded, so concurrent first requests
        never race to create two pools / writer threads / executors."""
        for name in ("db", "rbac", "directory", "replay", "audit", "knowledge_policy",
                     "knowledge_registry", "vocabulary", "context", "scope_policy",
                     "_activity_store", *(("verifier", "auth") if require_auth else ())):
            getattr(self, name)

    def validate(self, *, require_auth: bool = True) -> None:
        """Boot-time checks: unsafe access config, missing secrets, a schema behind
        the code. Raises — the process should not start."""
        _ = self.rbac   # role map / permission names (raises PolicyError)
        _ = self.vocabulary
        if require_auth:
            _ = self.auth   # issuers present, session secret long enough
        if self.db is not None and self.settings.database.require_current_schema:
            from .db.migrate import assert_current

            assert_current(self.db)
        self.warm(require_auth=require_auth)

    def close(self) -> None:
        if "_activity_store" in self.__dict__:
            self._activity_store[0].close()
        if self.db is not None:
            self.db.close()
