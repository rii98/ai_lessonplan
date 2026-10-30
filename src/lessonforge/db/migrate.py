"""Versioned SQL migrations and the ``lessonforge-db`` CLI.

Migrations are numbered plain-SQL files in ``db/migrations`` (``0001_initial.sql``,
``0002_…``). The code is raw ``psycopg`` with no ORM, so Alembic would pull in
SQLAlchemy just to run SQL; this runner gives the same guarantees in ~150 lines:

- **ordered & recorded** — each applied file is a row in ``schema_migrations``
  (version, name, sha256 checksum, when, how long);
- **atomic** — each file runs in its own transaction: all of it or none of it;
- **single-writer** — a session advisory lock serialises concurrent migrators
  (two replicas booting at once, a CI job racing a deploy);
- **drift-detected** — an applied file whose checksum changed is an error, not a
  silent skip: applied migrations are immutable, fix forward with a new file;
- **gated** — :func:`assert_current` lets the API refuse to start when the
  database is behind the code. Production never auto-migrates.

CLI::

    lessonforge-db migrate          # apply pending migrations (uses migrate_url)
    lessonforge-db status           # applied / pending / drifted, exit 1 if not current
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_FILE = re.compile(r"^(?P<version>\d{4})_(?P<name>[a-z0-9_]+)\.sql$")
# arbitrary but fixed: the advisory-lock key every lessonforge migrator agrees on
_LOCK_KEY = 0x1E550F09


class MigrationError(RuntimeError):
    pass


class SchemaOutOfDate(MigrationError):
    """The database is behind (or has drifted from) the code's migrations."""


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class Status:
    applied: list[int]
    pending: list[Migration]
    drifted: list[int]

    @property
    def current(self) -> bool:
        return not self.pending and not self.drifted


def discover(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    """Every migration file, ordered by version. Gaps/duplicates are an error."""
    found: list[Migration] = []
    for path in sorted(directory.glob("*.sql")):
        m = _FILE.match(path.name)
        if not m:
            raise MigrationError(f"bad migration filename {path.name!r} (want 0001_name.sql)")
        found.append(Migration(int(m["version"]), m["name"], path.read_text(encoding="utf-8")))
    versions = [m.version for m in found]
    if versions != list(range(1, len(found) + 1)):
        raise MigrationError(f"migration versions must be 1..N without gaps: {versions}")
    return found


_BOOKKEEPING = """
CREATE TABLE IF NOT EXISTS schema_migrations (
  version      int PRIMARY KEY,
  name         text NOT NULL,
  checksum     text NOT NULL,
  applied_at   timestamptz NOT NULL DEFAULT now(),
  execution_ms int NOT NULL
)"""


def _applied(conn: Any) -> dict[int, str]:
    exists = conn.execute("SELECT to_regclass('schema_migrations') IS NOT NULL").fetchone()
    if not _first(exists):
        return {}
    rows = conn.execute("SELECT version, checksum FROM schema_migrations").fetchall()
    return {int(_col(r, 0, "version")): str(_col(r, 1, "checksum")) for r in rows}


def status(conn: Any, migrations: list[Migration] | None = None) -> Status:
    migrations = discover() if migrations is None else migrations
    applied = _applied(conn)
    by_version = {m.version: m for m in migrations}
    drifted = sorted(v for v, c in applied.items() if v in by_version and by_version[v].checksum != c)
    pending = [m for m in migrations if m.version not in applied]
    return Status(applied=sorted(applied), pending=pending, drifted=drifted)


def migrate(conn: Any, migrations: list[Migration] | None = None, *, log=print) -> list[int]:
    """Apply every pending migration in order. ``conn`` must be in autocommit mode
    (each migration manages its own transaction). Returns the versions applied."""
    migrations = discover() if migrations is None else migrations
    conn.execute("SELECT pg_advisory_lock(%s)", (_LOCK_KEY,))
    try:
        conn.execute(_BOOKKEEPING)
        st = status(conn, migrations)  # re-read under the lock
        if st.drifted:
            raise MigrationError(
                f"applied migrations changed on disk: {st.drifted}. Applied migrations "
                "are immutable — revert the edit and add a new migration instead."
            )
        done: list[int] = []
        for m in st.pending:
            started = time.perf_counter()
            with conn.transaction():
                conn.execute(m.sql)
                conn.execute(
                    "INSERT INTO schema_migrations (version, name, checksum, execution_ms) "
                    "VALUES (%s, %s, %s, %s)",
                    (m.version, m.name, m.checksum, int((time.perf_counter() - started) * 1000)),
                )
            log(f"applied {m.version:04d}_{m.name}")
            done.append(m.version)
        return done
    finally:
        conn.execute("SELECT pg_advisory_unlock(%s)", (_LOCK_KEY,))


def assert_current(db: Any) -> None:
    """Raise :class:`SchemaOutOfDate` unless every migration is applied unchanged."""
    with db.tx() as cur:
        st = status(_CursorConn(cur))
    if not st.current:
        pending = [f"{m.version:04d}_{m.name}" for m in st.pending]
        raise SchemaOutOfDate(
            f"database schema is not current (pending={pending}, drifted={st.drifted}). "
            "Run `lessonforge-db migrate`."
        )


class _CursorConn:
    """Adapts a cursor to the tiny ``conn.execute(...).fetch*()`` surface used above."""

    def __init__(self, cur: Any) -> None:
        self._cur = cur

    def execute(self, sql: str, params: Any = None) -> Any:
        self._cur.execute(sql, params)
        return self._cur


def _first(row: Any) -> Any:
    return next(iter(row.values())) if isinstance(row, dict) else row[0]


def _col(row: Any, idx: int, name: str) -> Any:
    return row[name] if isinstance(row, dict) else row[idx]


# ── CLI ──────────────────────────────────────────────────────────────────────
def _connect(url: str) -> Any:
    import psycopg

    return psycopg.connect(url, autocommit=True, application_name="lessonforge-migrate")


def main(argv: list[str] | None = None) -> int:
    from ..config import load_settings

    parser = argparse.ArgumentParser(prog="lessonforge-db", description="Database migrations.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate", help="apply pending migrations")
    sub.add_parser("status", help="show applied/pending migrations; exit 1 if not current")
    args = parser.parse_args(argv)

    db = load_settings().database
    url = db.migrate_url or db.url
    if not url:
        print("DATABASE_URL (or DATABASE_MIGRATE_URL) is not set", file=sys.stderr)
        return 2
    with _connect(url) as conn:
        if args.cmd == "migrate":
            done = migrate(conn)
            print(f"{len(done)} migration(s) applied" if done else "schema already current")
            return 0
        st = status(conn)
        for v in st.applied:
            print(f"  applied  {v:04d}" + ("  ← DRIFTED" if v in st.drifted else ""))
        for m in st.pending:
            print(f"  pending  {m.version:04d}_{m.name}")
        print("current" if st.current else "NOT current")
        return 0 if st.current else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
