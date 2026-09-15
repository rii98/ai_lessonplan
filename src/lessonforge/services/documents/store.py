"""DocumentStore — where generated documents and their version history live.

A storage port behind the same registry pattern as :class:`ChatStore`: swap
``documents.provider`` in config and the backend changes with no code edit.

- ``memory``   — in-process dicts. Zero infra, non-persistent; tests + dev.
- ``sqlite``   — one file via stdlib ``sqlite3``. Persistent, no service.
- ``postgres`` — the Docker-backed store via ``psycopg``; DSN from config ``url``.

The two SQL backends share all row logic in :class:`_SqlDocumentStore`; they
differ only in how they connect and their placeholder token — the same
loose-coupling contract the chat store uses for persistence.

The store is deliberately dumb about *meaning*: it appends immutable versions,
moves a head pointer, and reads them back. All the version-graph policy (what a
refine records, how undo picks the parent) lives in the
:class:`~lessonforge.services.documents.service.DocumentService` on top.
"""

from __future__ import annotations

import json
import threading
from abc import ABC, abstractmethod
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ...config import DocumentStoreConfig
from ...domain.document import DocumentVersion, StoredDocument
from ..registry import register_document_store


class DocumentStore(ABC):
    """Persists documents and their append-only version chain, keyed by id. A
    missing document/version returns ``None`` (callers raise 404), never raises."""

    @abstractmethod
    def create(self, document: StoredDocument) -> StoredDocument: ...

    @abstractmethod
    def get(self, document_id: str) -> StoredDocument | None: ...

    @abstractmethod
    def list(self, owner_id: str = "default") -> list[StoredDocument]:
        """Documents for an owner, most-recently-updated first."""

    @abstractmethod
    def delete(self, document_id: str) -> bool:
        """Delete a document and all its versions. Returns whether it existed."""

    @abstractmethod
    def add_version(self, version: DocumentVersion) -> DocumentVersion:
        """Append an immutable version, point the document's head at it, and touch
        ``updated_at``. The version carries its own ``document_id``."""

    @abstractmethod
    def get_version(self, version_id: str) -> DocumentVersion | None: ...

    @abstractmethod
    def versions(self, document_id: str) -> list[DocumentVersion]:
        """All versions of a document, oldest first."""

    @abstractmethod
    def set_head(self, document_id: str, version_id: str) -> StoredDocument | None:
        """Move the head pointer (undo/redo). Returns the updated document, or
        ``None`` if the document or version is absent."""

    @classmethod
    def from_config(cls, cfg: DocumentStoreConfig) -> DocumentStore:  # pragma: no cover
        raise NotImplementedError


def _now() -> datetime:
    return datetime.now(UTC)


@register_document_store("memory")
class MemoryDocumentStore(DocumentStore):
    """In-process store. Fast, dependency-free, non-persistent. Thread-safe so a
    threaded dev server doesn't corrupt the lists mid-write."""

    def __init__(self) -> None:
        self._docs: dict[str, StoredDocument] = {}
        self._versions: dict[str, list[DocumentVersion]] = {}
        self._by_version: dict[str, DocumentVersion] = {}
        self._lock = threading.Lock()

    @classmethod
    def from_config(cls, cfg: DocumentStoreConfig) -> MemoryDocumentStore:
        return cls()

    def create(self, document: StoredDocument) -> StoredDocument:
        with self._lock:
            self._docs[document.id] = document
            self._versions.setdefault(document.id, [])
        return document

    def get(self, document_id: str) -> StoredDocument | None:
        return self._docs.get(document_id)

    def list(self, owner_id: str = "default") -> list[StoredDocument]:
        docs = [d for d in self._docs.values() if d.owner_id == owner_id]
        return sorted(docs, key=lambda d: d.updated_at, reverse=True)

    def delete(self, document_id: str) -> bool:
        with self._lock:
            existed = self._docs.pop(document_id, None) is not None
            for v in self._versions.pop(document_id, []):
                self._by_version.pop(v.id, None)
        return existed

    def add_version(self, version: DocumentVersion) -> DocumentVersion:
        with self._lock:
            doc = self._docs.get(version.document_id)
            if doc is None:
                raise KeyError(f"unknown document {version.document_id!r}")
            self._versions.setdefault(version.document_id, []).append(version)
            self._by_version[version.id] = version
            self._docs[version.document_id] = doc.model_copy(
                update={"head_version_id": version.id, "updated_at": _now()}
            )
        return version

    def get_version(self, version_id: str) -> DocumentVersion | None:
        return self._by_version.get(version_id)

    def versions(self, document_id: str) -> list[DocumentVersion]:
        return list(self._versions.get(document_id, []))

    def set_head(self, document_id: str, version_id: str) -> StoredDocument | None:
        with self._lock:
            doc = self._docs.get(document_id)
            v = self._by_version.get(version_id)
            if doc is None or v is None or v.document_id != document_id:
                return None
            updated = doc.model_copy(
                update={"head_version_id": version_id, "updated_at": _now()}
            )
            self._docs[document_id] = updated
            return updated


# ── SQL backends (sqlite + postgres share everything but connection + dialect) ──
class _SqlDocumentStore(DocumentStore):
    """Shared SQL implementation. Queryable columns are native; the nested,
    free-form parts (snapshot, diff, grounding_sources, scores) are JSON text —
    dialect-agnostic, so one row-logic serves sqlite and postgres. Subclasses
    supply :meth:`_connect` and the placeholder token ``_ph``."""

    _ph = "?"

    @contextmanager
    def _connect(self):  # pragma: no cover - overridden
        raise NotImplementedError
        yield

    def _q(self, sql: str) -> str:
        return sql.replace("?", self._ph) if self._ph != "?" else sql

    def _init_schema(self) -> None:
        with self._connect() as (conn, cur):
            cur.execute(
                "CREATE TABLE IF NOT EXISTS documents ("
                " id TEXT PRIMARY KEY,"
                " owner_id TEXT NOT NULL,"
                " kind TEXT NOT NULL,"
                " title TEXT NOT NULL,"
                " grade INTEGER,"
                " subject TEXT,"
                " head_version_id TEXT,"
                " created_at TEXT NOT NULL,"
                " updated_at TEXT NOT NULL)"
            )
            cur.execute(
                "CREATE TABLE IF NOT EXISTS document_versions ("
                " id TEXT PRIMARY KEY,"
                " document_id TEXT NOT NULL,"
                " ord INTEGER NOT NULL,"
                " parent_id TEXT,"
                " origin TEXT NOT NULL,"
                " snapshot TEXT NOT NULL,"
                " instruction TEXT NOT NULL,"
                " diff TEXT NOT NULL,"
                " grounding_sources TEXT NOT NULL,"
                " scores TEXT NOT NULL,"
                " created_at TEXT NOT NULL)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS ix_document_versions_doc "
                "ON document_versions (document_id, ord)"
            )
            conn.commit()

    # ── (de)serialization ────────────────────────────────────────────────────
    @staticmethod
    def _doc_row(d: StoredDocument) -> tuple:
        return (
            d.id, d.owner_id, d.kind.value, d.title, d.grade, d.subject,
            d.head_version_id, d.created_at.isoformat(), d.updated_at.isoformat(),
        )

    @staticmethod
    def _doc_from_row(r: Any) -> StoredDocument:
        return StoredDocument(
            id=r[0], owner_id=r[1], kind=r[2], title=r[3], grade=r[4], subject=r[5],
            head_version_id=r[6], created_at=r[7], updated_at=r[8],
        )

    @staticmethod
    def _version_from_row(r: Any) -> DocumentVersion:
        return DocumentVersion(
            id=r[0], document_id=r[1], parent_id=r[2], origin=r[3],
            snapshot=json.loads(r[4]), instruction=r[5], diff=json.loads(r[6]),
            grounding_sources=json.loads(r[7]), scores=json.loads(r[8]), created_at=r[9],
        )

    _VCOLS = (
        "id, document_id, parent_id, origin, snapshot, instruction, diff, "
        "grounding_sources, scores, created_at"
    )

    # ── CRUD ─────────────────────────────────────────────────────────────────
    def create(self, document: StoredDocument) -> StoredDocument:
        with self._connect() as (conn, cur):
            cur.execute(
                self._q(
                    "INSERT INTO documents (id, owner_id, kind, title, grade, subject, "
                    "head_version_id, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
                ),
                self._doc_row(document),
            )
            conn.commit()
        return document

    def get(self, document_id: str) -> StoredDocument | None:
        with self._connect() as (_conn, cur):
            cur.execute(
                self._q(
                    "SELECT id, owner_id, kind, title, grade, subject, head_version_id, "
                    "created_at, updated_at FROM documents WHERE id = ?"
                ),
                (document_id,),
            )
            row = cur.fetchone()
        return self._doc_from_row(row) if row else None

    def list(self, owner_id: str = "default") -> list[StoredDocument]:
        with self._connect() as (_conn, cur):
            cur.execute(
                self._q(
                    "SELECT id, owner_id, kind, title, grade, subject, head_version_id, "
                    "created_at, updated_at FROM documents WHERE owner_id = ? "
                    "ORDER BY updated_at DESC"
                ),
                (owner_id,),
            )
            rows = cur.fetchall()
        return [self._doc_from_row(r) for r in rows]

    def delete(self, document_id: str) -> bool:
        with self._connect() as (conn, cur):
            cur.execute(self._q("SELECT 1 FROM documents WHERE id = ?"), (document_id,))
            existed = cur.fetchone() is not None
            cur.execute(
                self._q("DELETE FROM document_versions WHERE document_id = ?"), (document_id,)
            )
            cur.execute(self._q("DELETE FROM documents WHERE id = ?"), (document_id,))
            conn.commit()
        return existed

    def add_version(self, version: DocumentVersion) -> DocumentVersion:
        with self._connect() as (conn, cur):
            cur.execute(
                self._q("SELECT 1 FROM documents WHERE id = ?"), (version.document_id,)
            )
            if cur.fetchone() is None:
                raise KeyError(f"unknown document {version.document_id!r}")
            cur.execute(
                self._q("SELECT COALESCE(MAX(ord), -1) + 1 FROM document_versions "
                        "WHERE document_id = ?"),
                (version.document_id,),
            )
            ord_ = int(cur.fetchone()[0])
            cur.execute(
                self._q(
                    "INSERT INTO document_versions (id, document_id, ord, parent_id, origin, "
                    "snapshot, instruction, diff, grounding_sources, scores, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                ),
                (
                    version.id, version.document_id, ord_, version.parent_id,
                    version.origin.value, json.dumps(version.snapshot), version.instruction,
                    json.dumps(version.diff), json.dumps(version.grounding_sources),
                    json.dumps(version.scores), version.created_at.isoformat(),
                ),
            )
            cur.execute(
                self._q("UPDATE documents SET head_version_id = ?, updated_at = ? WHERE id = ?"),
                (version.id, _now().isoformat(), version.document_id),
            )
            conn.commit()
        return version

    def get_version(self, version_id: str) -> DocumentVersion | None:
        with self._connect() as (_conn, cur):
            cur.execute(
                self._q(f"SELECT {self._VCOLS} FROM document_versions WHERE id = ?"),
                (version_id,),
            )
            row = cur.fetchone()
        return self._version_from_row(row) if row else None

    def versions(self, document_id: str) -> list[DocumentVersion]:
        with self._connect() as (_conn, cur):
            cur.execute(
                self._q(
                    f"SELECT {self._VCOLS} FROM document_versions "
                    "WHERE document_id = ? ORDER BY ord ASC"
                ),
                (document_id,),
            )
            rows = cur.fetchall()
        return [self._version_from_row(r) for r in rows]

    def set_head(self, document_id: str, version_id: str) -> StoredDocument | None:
        with self._connect() as (conn, cur):
            cur.execute(
                self._q("SELECT 1 FROM document_versions WHERE id = ? AND document_id = ?"),
                (version_id, document_id),
            )
            if cur.fetchone() is None:
                return None
            cur.execute(
                self._q("UPDATE documents SET head_version_id = ?, updated_at = ? WHERE id = ?"),
                (version_id, _now().isoformat(), document_id),
            )
            conn.commit()
        return self.get(document_id)


@register_document_store("sqlite")
class SqliteDocumentStore(_SqlDocumentStore):
    """One SQLite file. Persistent, no service, no new dependency. Serialized
    access (one connection + a lock) keeps a threaded dev server safe."""

    def __init__(self, path: str | Path) -> None:
        import sqlite3

        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._lock = threading.Lock()
        self._init_schema()

    @classmethod
    def from_config(cls, cfg: DocumentStoreConfig) -> SqliteDocumentStore:
        return cls(cfg.path or ".lessonforge/documents.db")

    @contextmanager
    def _connect(self):
        with self._lock:
            yield self._conn, self._conn.cursor()


@register_document_store("postgres")
class PostgresDocumentStore(_SqlDocumentStore):
    """The Docker-backed store via ``psycopg`` (v3). DSN from config ``url``. A
    short-lived connection per operation keeps it robust to the DB restarting."""

    _ph = "%s"

    def __init__(self, dsn: str) -> None:
        if not dsn:
            raise ValueError(
                "postgres document store needs a DSN — set documents.url (e.g. ${DOCS_DB_URL})."
            )
        self.dsn = dsn
        self._init_schema()

    @classmethod
    def from_config(cls, cfg: DocumentStoreConfig) -> PostgresDocumentStore:
        return cls(cfg.url or "")

    @contextmanager
    def _connect(self):
        import psycopg

        conn = psycopg.connect(self.dsn)
        try:
            yield conn, conn.cursor()
        finally:
            conn.close()
