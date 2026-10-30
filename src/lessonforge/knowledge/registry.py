"""KnowledgeRegistry — the shelves (``knowledge_spaces``) and what is on them
(``corpus_sources``: one row per ingested document).

With it, corpus management can list "School A's documents", delete one cleanly,
or re-index a space (needed when the embedder changes) without scanning the
vector store. A school's space is created on its first upload.
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

from ..db.pool import Database, as_uuid
from .scope import GLOBAL_SPACE, valid_space


@dataclass(frozen=True, slots=True)
class Space:
    id: str
    kind: str
    tenant_id: str | None
    name: str
    status: str = "active"


@dataclass(frozen=True, slots=True)
class CorpusSource:
    space_id: str
    collection: str
    doc_id: str
    title: str
    checksum: str
    chunks: int
    source_label: str | None = None
    ingested_by: str | None = None
    ingested_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def view(self) -> dict[str, Any]:
        return {"space": self.space_id, "collection": self.collection, "doc_id": self.doc_id,
                "title": self.title, "source_label": self.source_label,
                "checksum": self.checksum, "chunks": self.chunks,
                "ingested_at": self.ingested_at}


def space_record(space_id: str, name: str | None = None) -> Space:
    if not valid_space(space_id):
        raise ValueError(f"invalid knowledge space {space_id!r}")
    if space_id == GLOBAL_SPACE:
        return Space(GLOBAL_SPACE, "global", None, name or "Shared curriculum")
    tenant = space_id.split(":", 1)[1]
    return Space(space_id, "school", tenant, name or f"School {tenant}")


class KnowledgeRegistry(ABC):
    @abstractmethod
    def ensure_space(self, space_id: str, name: str | None = None) -> Space: ...

    @abstractmethod
    def spaces(self) -> list[Space]: ...

    @abstractmethod
    def record_sources(self, rows: list[CorpusSource]) -> None:
        """Upsert one row per ingested document (re-ingest updates in place)."""

    @abstractmethod
    def list_sources(self, *, spaces: list[str] | None, collection: str | None,
                     limit: int, offset: int = 0) -> list[CorpusSource]:
        """``spaces=None`` means every space (callers pass the caller's scope)."""

    @abstractmethod
    def remove_source(self, space_id: str, collection: str, doc_id: str) -> CorpusSource | None: ...


class MemoryKnowledgeRegistry(KnowledgeRegistry):
    def __init__(self) -> None:
        self._spaces: dict[str, Space] = {GLOBAL_SPACE: space_record(GLOBAL_SPACE)}
        self._sources: dict[tuple[str, str, str], CorpusSource] = {}
        self._lock = threading.Lock()

    def ensure_space(self, space_id, name=None):
        with self._lock:
            return self._spaces.setdefault(space_id, space_record(space_id, name))

    def spaces(self):
        return sorted(self._spaces.values(), key=lambda s: (s.kind != "global", s.id))

    def record_sources(self, rows):
        with self._lock:
            for r in rows:
                self._sources[(r.space_id, r.collection, r.doc_id)] = replace(
                    r, ingested_at=datetime.now(UTC))

    def list_sources(self, *, spaces, collection, limit, offset=0):
        rows = [r for r in self._sources.values()
                if (spaces is None or r.space_id in spaces)
                and (collection is None or r.collection == collection)]
        rows.sort(key=lambda r: (r.ingested_at, r.doc_id), reverse=True)
        return rows[offset:offset + limit]

    def remove_source(self, space_id, collection, doc_id):
        with self._lock:
            return self._sources.pop((space_id, collection, doc_id), None)


class PostgresKnowledgeRegistry(KnowledgeRegistry):
    def __init__(self, db: Database) -> None:
        self.db = db

    def ensure_space(self, space_id, name=None):
        sp = space_record(space_id, name)
        with self.db.tx() as cur:
            cur.execute(
                "INSERT INTO knowledge_spaces (id, kind, tenant_id, name) VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (id) DO NOTHING", (sp.id, sp.kind, sp.tenant_id, sp.name))
        return sp

    def spaces(self):
        with self.db.tx() as cur:
            cur.execute("SELECT id, kind, tenant_id, name, status FROM knowledge_spaces "
                        "ORDER BY kind <> 'global', id")
            return [Space(r["id"], r["kind"], r["tenant_id"], r["name"], r["status"])
                    for r in cur.fetchall()]

    def record_sources(self, rows):
        if not rows:
            return
        with self.db.tx() as cur:
            cur.executemany(
                """INSERT INTO corpus_sources (space_id, collection, doc_id, title, source_label,
                                               checksum, chunks, ingested_by)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (space_id, collection, doc_id) DO UPDATE SET
                     title = EXCLUDED.title, source_label = EXCLUDED.source_label,
                     checksum = EXCLUDED.checksum, chunks = EXCLUDED.chunks,
                     ingested_by = EXCLUDED.ingested_by, ingested_at = now()""",
                [(r.space_id, r.collection, r.doc_id, r.title, r.source_label, r.checksum,
                  r.chunks, as_uuid(r.ingested_by)) for r in rows],
            )

    def list_sources(self, *, spaces, collection, limit, offset=0):
        sql, params = [], []
        if spaces is not None:
            sql.append("space_id = ANY(%s)")
            params.append(list(spaces))
        if collection is not None:
            sql.append("collection = %s")
            params.append(collection)
        where = f"WHERE {' AND '.join(sql)}" if sql else ""
        with self.db.tx() as cur:
            cur.execute(
                "SELECT space_id, collection, doc_id, title, source_label, checksum, chunks, "
                f"ingested_by, ingested_at FROM corpus_sources {where} "
                "ORDER BY ingested_at DESC, doc_id DESC LIMIT %s OFFSET %s",
                [*params, limit, offset])
            return [_source(r) for r in cur.fetchall()]

    def remove_source(self, space_id, collection, doc_id):
        with self.db.tx() as cur:
            cur.execute(
                "DELETE FROM corpus_sources WHERE space_id = %s AND collection = %s AND doc_id = %s "
                "RETURNING space_id, collection, doc_id, title, source_label, checksum, chunks, "
                "ingested_by, ingested_at", (space_id, collection, doc_id))
            row = cur.fetchone()
        return _source(row) if row else None


def _source(r: dict[str, Any]) -> CorpusSource:
    return CorpusSource(
        space_id=r["space_id"], collection=r["collection"], doc_id=r["doc_id"], title=r["title"],
        source_label=r["source_label"], checksum=r["checksum"], chunks=r["chunks"],
        ingested_by=str(r["ingested_by"]) if r["ingested_by"] else None,
        ingested_at=r["ingested_at"])
