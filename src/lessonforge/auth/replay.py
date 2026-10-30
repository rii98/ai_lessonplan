"""Launch-token replay guard: a ``jti`` is accepted exactly once.

``consume`` is a single atomic statement (``INSERT … ON CONFLICT DO NOTHING``), so
two concurrent launches with the same token cannot both win. Rows are purged
after the token's own expiry (``lessonforge-admin purge``).
"""

from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod
from datetime import UTC, datetime

from ..db.pool import Database


class ReplayGuard(ABC):
    @abstractmethod
    def consume(self, issuer: str, jti: str, expires_at: float) -> bool:
        """Record the jti; ``True`` the first time, ``False`` on any reuse."""

    @abstractmethod
    def purge(self) -> int:
        """Delete expired entries; returns how many."""


class MemoryReplayGuard(ReplayGuard):
    def __init__(self) -> None:
        self._seen: dict[tuple[str, str], float] = {}
        self._lock = threading.Lock()

    def consume(self, issuer: str, jti: str, expires_at: float) -> bool:
        with self._lock:
            if (issuer, jti) in self._seen:
                return False
            self._seen[(issuer, jti)] = expires_at
            return True

    def purge(self) -> int:
        now = time.time()
        with self._lock:
            dead = [k for k, exp in self._seen.items() if exp < now]
            for k in dead:
                del self._seen[k]
        return len(dead)


class PostgresReplayGuard(ReplayGuard):
    def __init__(self, db: Database) -> None:
        self.db = db

    def consume(self, issuer: str, jti: str, expires_at: float) -> bool:
        with self.db.tx() as cur:
            cur.execute(
                "INSERT INTO used_token_ids (issuer, jti, expires_at) VALUES (%s, %s, %s) "
                "ON CONFLICT DO NOTHING RETURNING 1",
                (issuer, jti, datetime.fromtimestamp(expires_at, UTC)),
            )
            return cur.fetchone() is not None

    def purge(self) -> int:
        with self.db.tx() as cur:
            cur.execute("DELETE FROM used_token_ids WHERE expires_at < now()")
            return cur.rowcount
