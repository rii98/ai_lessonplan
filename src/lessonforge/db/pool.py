"""Database — the single Postgres connection pool every store shares (D10).

One ``psycopg_pool.ConnectionPool`` per process, built once by the container and
injected into each Postgres adapter. It replaces the old connect-per-operation
stores (one TCP + auth handshake per query) and their separate DSNs.

Conventions every Postgres adapter follows:

- ``with db.tx() as cur:`` — one transaction, committed on success, rolled back
  on any exception. Reads use it too (a single short transaction is the cheapest
  consistent read).
- rows come back as ``dict`` (``dict_row``) so adapters read columns by name and
  a column added by a migration never shifts positional indexes.
- ids are ``uuid`` in the database and canonical strings in the domain;
  :func:`as_uuid` turns an untrusted id from a URL into a ``UUID`` or ``None`` so
  a malformed id is a clean 404, never a 500 from a cast error.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from ..config import DatabaseConfig

log = logging.getLogger(__name__)


class DatabaseUnavailable(RuntimeError):
    """The pool could not be opened or a connection could not be obtained."""


class Database:
    """A lazily-opened, health-checked connection pool."""

    def __init__(self, cfg: DatabaseConfig, *, application_name: str = "lessonforge-api") -> None:
        if not cfg.url:
            raise ValueError(
                "database.url is empty — set DATABASE_URL (postgresql://user:pass@host/db)."
            )
        self.cfg = cfg
        self._application_name = application_name
        self._pool: Any = None

    # ── lifecycle ────────────────────────────────────────────────────────────
    def _open(self) -> Any:
        if self._pool is None:
            from psycopg.rows import dict_row
            from psycopg_pool import ConnectionPool

            options = f"-c statement_timeout={self.cfg.statement_timeout_ms}"
            self._pool = ConnectionPool(
                self.cfg.url,
                min_size=self.cfg.pool_min,
                max_size=self.cfg.pool_max,
                timeout=self.cfg.pool_timeout_s,
                # validate on checkout so a restarted Postgres costs one retry, not
                # a burst of 500s from stale connections.
                check=ConnectionPool.check_connection,
                kwargs={
                    "row_factory": dict_row,
                    "application_name": self._application_name,
                    "options": options,
                },
                name="lessonforge",
                open=False,
            )
            self._pool.open(wait=False)
        return self._pool

    def close(self) -> None:
        if self._pool is not None:
            self._pool.close()
            self._pool = None

    # ── access ───────────────────────────────────────────────────────────────
    @contextmanager
    def tx(self) -> Iterator[Any]:
        """A cursor inside one transaction (commit on success, rollback on error).
        Pool exhaustion and lost connections surface as :class:`DatabaseUnavailable`
        (→ HTTP 503); every other error propagates unchanged."""
        from psycopg import OperationalError
        from psycopg_pool import PoolTimeout

        try:
            with self._open().connection() as conn, conn.transaction(), conn.cursor() as cur:
                yield cur
        except (PoolTimeout, OperationalError) as exc:
            raise DatabaseUnavailable(str(exc)) from exc

    def health(self) -> bool:
        try:
            with self.tx() as cur:
                cur.execute("SELECT 1")
                return cur.fetchone() is not None
        except Exception:
            return False


# ── value helpers shared by every adapter ────────────────────────────────────
def as_uuid(value: Any) -> uuid.UUID | None:
    """Parse an untrusted id (path parameter, cursor) into a UUID, or ``None``."""
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


def id_str(value: Any) -> str | None:
    """A database uuid back into the domain's canonical string form."""
    return None if value is None else str(value)


def jsonb(value: Any) -> Any:
    """Wrap a Python value for a ``jsonb`` parameter."""
    from psycopg.types.json import Jsonb

    return Jsonb(value)
