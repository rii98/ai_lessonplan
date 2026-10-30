"""DocumentStore — generated documents and their append-only version history.

A storage port behind the registry pattern: ``documents.provider`` picks ``memory``
(tests) or ``postgres`` (the shared pool). One contract suite runs against both.

The store is deliberately dumb about *meaning*: it appends immutable versions,
moves a head pointer, and reads them back. Version-graph policy and read access
(owner, or same-school colleague for a shared plan) live in
:class:`~lessonforge.services.documents.service.DocumentService`. The store adds
defence in depth: **every write is guarded by ``owner_id`` in its WHERE clause**,
so even a service bug cannot modify someone else's document.

Efficiency: a document is created together with its first version in one
transaction; the head is read with its version in one query; history lists never
load snapshots; ``ord`` comes from an atomic counter; lists are keyset-paged.
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from typing import Any

from ...config import DocumentStoreConfig
from ...db.pool import Database, as_uuid, contains_pattern, id_str, jsonb
from ...domain.document import (
    DocumentKind,
    DocumentVersion,
    DocumentVersionInfo,
    StoredDocument,
    Visibility,
)
from ...pagination import Keyset, Page, page_of
from ..registry import register_document_store


def _now() -> datetime:
    return datetime.now(UTC)


class DocumentStore(ABC):
    @abstractmethod
    def create_with_version(self, document: StoredDocument,
                            version: DocumentVersion) -> tuple[StoredDocument, DocumentVersion]:
        """Atomically create a document and its first version (the head)."""

    @abstractmethod
    def get(self, document_id: str) -> StoredDocument | None:
        """Unscoped read — the service applies the access rule."""

    @abstractmethod
    def head(self, document_id: str) -> tuple[StoredDocument, DocumentVersion | None] | None:
        """The document and its head version, in one read."""

    @abstractmethod
    def list_owned(self, owner_id: str, *, kind: DocumentKind | None = None, limit: int,
                   cursor: Keyset | None = None) -> Page[StoredDocument]: ...

    @abstractmethod
    def list_shared(self, tenant_id: str, *, kind: DocumentKind | None = None,
                    grade: int | None = None, subject: str | None = None, q: str | None = None,
                    limit: int, cursor: Keyset | None = None) -> Page[StoredDocument]:
        """Documents shared with ``tenant_id``'s school, newest share first, each with
        its owner's display name."""

    @abstractmethod
    def add_version(self, version: DocumentVersion, *, owner_id: str) -> DocumentVersion:
        """Append a version and make it the head. ``KeyError`` if the document does
        not exist or is not owned by ``owner_id``."""

    @abstractmethod
    def get_version(self, document_id: str, version_id: str) -> DocumentVersion | None: ...

    @abstractmethod
    def versions(self, document_id: str) -> list[DocumentVersionInfo]:
        """The history, oldest first, without snapshots."""

    @abstractmethod
    def latest_child(self, document_id: str, parent_id: str) -> DocumentVersionInfo | None:
        """The most recent version whose parent is ``parent_id`` (redo target)."""

    @abstractmethod
    def set_head(self, document_id: str, version_id: str, *, owner_id: str) -> StoredDocument | None: ...

    @abstractmethod
    def set_visibility(self, document_id: str, visibility: Visibility, *,
                       owner_id: str) -> StoredDocument | None: ...

    @abstractmethod
    def delete(self, document_id: str, *, owner_id: str) -> bool: ...

    @abstractmethod
    def counts(self) -> dict[str, int]:
        """Totals for the owner console: documents per kind, and shared ones."""

    @classmethod
    def from_config(cls, cfg: DocumentStoreConfig, *, db: Database | None = None) -> DocumentStore:  # pragma: no cover
        raise NotImplementedError


def _updated_key(d: StoredDocument) -> Keyset:
    return Keyset(d.updated_at, d.id)


def _shared_key(d: StoredDocument) -> Keyset:
    return Keyset(d.shared_at or d.updated_at, d.id)


# ── memory ───────────────────────────────────────────────────────────────────
@register_document_store("memory")
class MemoryDocumentStore(DocumentStore):
    def __init__(self) -> None:
        self._docs: dict[str, StoredDocument] = {}
        self._versions: dict[str, list[DocumentVersion]] = {}
        self._by_version: dict[str, DocumentVersion] = {}
        self.owner_names: dict[str, str] = {}   # tests set display names here
        self._lock = threading.RLock()

    @classmethod
    def from_config(cls, cfg: DocumentStoreConfig, *, db: Database | None = None) -> MemoryDocumentStore:
        return cls()

    def create_with_version(self, document, version):
        with self._lock:
            doc = document.model_copy(update={"head_version_id": version.id})
            self._docs[doc.id] = doc
            self._versions[doc.id] = [version]
            self._by_version[version.id] = version
            return doc, version

    def get(self, document_id):
        return self._docs.get(document_id)

    def head(self, document_id):
        doc = self._docs.get(document_id)
        if doc is None:
            return None
        return doc, self._by_version.get(doc.head_version_id or "")

    def list_owned(self, owner_id, *, kind=None, limit, cursor=None):
        docs = sorted((d for d in self._docs.values()
                       if d.owner_id == owner_id and (kind is None or d.kind == kind)),
                      key=lambda d: (d.updated_at, d.id), reverse=True)
        if cursor is not None:
            docs = [d for d in docs if cursor.before(d.updated_at, d.id)]
        return page_of(docs[:limit + 1], limit, _updated_key)

    def list_shared(self, tenant_id, *, kind=None, grade=None, subject=None, q=None, limit,
                    cursor=None):
        needle = (q or "").casefold()
        docs = [d.model_copy(update={"owner_name": self.owner_names.get(d.owner_id)})
                for d in self._docs.values()
                if d.visibility == Visibility.school and d.tenant_id == tenant_id
                and (kind is None or d.kind == kind) and (grade is None or d.grade == grade)
                and (subject is None or d.subject == subject)
                and (not needle or needle in d.title.casefold())]
        docs.sort(key=lambda d: (d.shared_at, d.id), reverse=True)
        if cursor is not None:
            docs = [d for d in docs if cursor.before(d.shared_at, d.id)]
        return page_of(docs[:limit + 1], limit, _shared_key)

    def add_version(self, version, *, owner_id):
        with self._lock:
            doc = self._docs.get(version.document_id)
            if doc is None or doc.owner_id != owner_id:
                raise KeyError(f"unknown document {version.document_id!r}")
            self._versions[doc.id].append(version)
            self._by_version[version.id] = version
            self._docs[doc.id] = doc.model_copy(
                update={"head_version_id": version.id, "updated_at": _now()})
            return version

    def get_version(self, document_id, version_id):
        v = self._by_version.get(version_id)
        return v if v is not None and v.document_id == document_id else None

    def versions(self, document_id):
        return [DocumentVersionInfo.of(v) for v in self._versions.get(document_id, [])]

    def latest_child(self, document_id, parent_id):
        kids = [v for v in self._versions.get(document_id, []) if v.parent_id == parent_id]
        return DocumentVersionInfo.of(max(kids, key=lambda v: v.created_at)) if kids else None

    def set_head(self, document_id, version_id, *, owner_id):
        with self._lock:
            doc = self._docs.get(document_id)
            v = self._by_version.get(version_id)
            if doc is None or doc.owner_id != owner_id or v is None or v.document_id != document_id:
                return None
            doc = doc.model_copy(update={"head_version_id": version_id, "updated_at": _now()})
            self._docs[document_id] = doc
            return doc

    def set_visibility(self, document_id, visibility, *, owner_id):
        with self._lock:
            doc = self._docs.get(document_id)
            if doc is None or doc.owner_id != owner_id:
                return None
            if visibility == Visibility.school and doc.tenant_id is None:
                raise ValueError("a document without a school cannot be shared")
            shared_at = (doc.shared_at or _now()) if visibility == Visibility.school else None
            doc = doc.model_copy(update={"visibility": visibility, "shared_at": shared_at})
            self._docs[document_id] = doc
            return doc

    def counts(self):
        docs = list(self._docs.values())
        return {"lesson": sum(d.kind == DocumentKind.lesson for d in docs),
                "unit": sum(d.kind == DocumentKind.unit for d in docs),
                "shared": sum(d.visibility == Visibility.school for d in docs)}

    def delete(self, document_id, *, owner_id):
        with self._lock:
            doc = self._docs.get(document_id)
            if doc is None or doc.owner_id != owner_id:
                return False
            del self._docs[document_id]
            for v in self._versions.pop(document_id, []):
                self._by_version.pop(v.id, None)
            for other_id, other in list(self._docs.items()):   # ON DELETE SET NULL
                if other.copied_from == document_id:
                    self._docs[other_id] = other.model_copy(update={"copied_from": None})
            return True


# ── postgres ─────────────────────────────────────────────────────────────────
_DOC = ("d.id, d.owner_id, d.tenant_id, d.kind, d.title, d.grade, d.subject, d.head_version_id, "
        "d.visibility, d.shared_at, d.copied_from, d.created_at, d.updated_at")
_VINFO = ("v.id, v.document_id, v.parent_id, v.origin, v.instruction, v.diff, "
          "v.grounding_sources, v.scores, v.created_at")


def _doc(r: dict[str, Any]) -> StoredDocument:
    return StoredDocument(
        id=str(r["id"]), owner_id=str(r["owner_id"]), tenant_id=r["tenant_id"], kind=r["kind"],
        title=r["title"], grade=r["grade"], subject=r["subject"],
        head_version_id=id_str(r["head_version_id"]), visibility=r["visibility"],
        shared_at=r["shared_at"], copied_from=id_str(r["copied_from"]),
        owner_name=r.get("owner_name"), created_at=r["created_at"], updated_at=r["updated_at"])


def _vinfo(r: dict[str, Any]) -> DocumentVersionInfo:
    return DocumentVersionInfo(
        id=str(r["id"]), document_id=str(r["document_id"]), parent_id=id_str(r["parent_id"]),
        origin=r["origin"], instruction=r["instruction"], diff=r["diff"],
        grounding_sources=r["grounding_sources"], scores=r["scores"], created_at=r["created_at"])


def _version(r: dict[str, Any]) -> DocumentVersion:
    return DocumentVersion(**_vinfo(r).model_dump(), snapshot=r["snapshot"])


@register_document_store("postgres")
class PostgresDocumentStore(DocumentStore):
    def __init__(self, db: Database) -> None:
        self.db = db

    @classmethod
    def from_config(cls, cfg: DocumentStoreConfig, *, db: Database | None = None) -> PostgresDocumentStore:
        if db is None:
            raise ValueError("the postgres document store needs database.url (DATABASE_URL)")
        return cls(db)

    def create_with_version(self, document, version):
        d, v = document, version
        with self.db.tx() as cur:   # head FK is DEFERRABLE: insert both, check at commit
            cur.execute(
                "INSERT INTO documents (id, owner_id, tenant_id, kind, title, grade, subject, "
                "head_version_id, version_count, visibility, copied_from, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 1, 'private', %s, %s, %s)",
                (as_uuid(d.id), as_uuid(d.owner_id), d.tenant_id, d.kind.value, d.title, d.grade,
                 d.subject, as_uuid(v.id), as_uuid(d.copied_from), d.created_at, d.updated_at))
            self._insert_version(cur, v, ord_=0)
        return d.model_copy(update={"head_version_id": v.id}), v

    @staticmethod
    def _insert_version(cur: Any, v: DocumentVersion, *, ord_: int) -> None:
        cur.execute(
            "INSERT INTO document_versions (id, document_id, ord, parent_id, origin, snapshot, "
            "instruction, diff, grounding_sources, scores, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (as_uuid(v.id), as_uuid(v.document_id), ord_, as_uuid(v.parent_id), v.origin.value,
             jsonb(v.snapshot), v.instruction, jsonb(v.diff), jsonb(v.grounding_sources),
             jsonb(v.scores), v.created_at))

    def get(self, document_id):
        did = as_uuid(document_id)
        if did is None:
            return None
        with self.db.tx() as cur:
            cur.execute(f"SELECT {_DOC} FROM documents d WHERE d.id = %s", (did,))
            row = cur.fetchone()
        return _doc(row) if row else None

    def head(self, document_id):
        did = as_uuid(document_id)
        if did is None:
            return None
        with self.db.tx() as cur:
            cur.execute(
                f"SELECT {_DOC}, v.id AS v_id, v.document_id AS v_document_id, "
                "v.parent_id AS v_parent_id, v.origin AS v_origin, v.snapshot AS v_snapshot, "
                "v.instruction AS v_instruction, v.diff AS v_diff, "
                "v.grounding_sources AS v_grounding_sources, v.scores AS v_scores, "
                "v.created_at AS v_created_at "
                "FROM documents d LEFT JOIN document_versions v ON v.id = d.head_version_id "
                "WHERE d.id = %s", (did,))
            row = cur.fetchone()
        if row is None:
            return None
        version = None
        if row["v_id"] is not None:
            version = _version({k[2:]: val for k, val in row.items() if k.startswith("v_")})
        return _doc(row), version

    def list_owned(self, owner_id, *, kind=None, limit, cursor=None):
        oid = as_uuid(owner_id)
        if oid is None:
            return Page()
        sql = f"SELECT {_DOC} FROM documents d WHERE d.owner_id = %s"
        params: list[Any] = [oid]
        if kind is not None:
            sql += " AND d.kind = %s"
            params.append(kind.value)
        if cursor is not None:
            sql += " AND (d.updated_at, d.id) < (%s, %s)"
            params += [cursor.ts, as_uuid(cursor.id)]
        sql += " ORDER BY d.updated_at DESC, d.id DESC LIMIT %s"
        params.append(limit + 1)
        with self.db.tx() as cur:
            cur.execute(sql, params)
            rows = [_doc(r) for r in cur.fetchall()]
        return page_of(rows, limit, _updated_key)

    def list_shared(self, tenant_id, *, kind=None, grade=None, subject=None, q=None, limit,
                    cursor=None):
        sql = (f"SELECT {_DOC}, p.display_name AS owner_name FROM documents d "
               "JOIN principals p ON p.id = d.owner_id "
               "WHERE d.visibility = 'school' AND d.tenant_id = %s")
        params: list[Any] = [tenant_id]
        for col, val in (("kind", kind.value if kind else None), ("grade", grade),
                         ("subject", subject)):
            if val is not None:
                sql += f" AND d.{col} = %s"
                params.append(val)
        if q:
            sql += " AND d.title ILIKE %s"
            params.append(contains_pattern(q))
        if cursor is not None:
            sql += " AND (d.shared_at, d.id) < (%s, %s)"
            params += [cursor.ts, as_uuid(cursor.id)]
        sql += " ORDER BY d.shared_at DESC, d.id DESC LIMIT %s"
        params.append(limit + 1)
        with self.db.tx() as cur:
            cur.execute(sql, params)
            rows = [_doc(r) for r in cur.fetchall()]
        return page_of(rows, limit, _shared_key)

    def add_version(self, version, *, owner_id):
        with self.db.tx() as cur:
            # one atomic statement allocates ord, moves the head and locks the row,
            # so concurrent commits on one document serialise cleanly.
            cur.execute(
                "UPDATE documents SET version_count = version_count + 1, head_version_id = %s, "
                "updated_at = now() WHERE id = %s AND owner_id = %s "
                "RETURNING version_count - 1 AS ord",
                (as_uuid(version.id), as_uuid(version.document_id), as_uuid(owner_id)))
            row = cur.fetchone()
            if row is None:
                raise KeyError(f"unknown document {version.document_id!r}")
            self._insert_version(cur, version, ord_=row["ord"])
        return version

    def get_version(self, document_id, version_id):
        did, vid = as_uuid(document_id), as_uuid(version_id)
        if did is None or vid is None:
            return None
        with self.db.tx() as cur:
            cur.execute(f"SELECT {_VINFO}, v.snapshot FROM document_versions v "
                        "WHERE v.id = %s AND v.document_id = %s", (vid, did))
            row = cur.fetchone()
        return _version(row) if row else None

    def versions(self, document_id):
        with self.db.tx() as cur:
            cur.execute(f"SELECT {_VINFO} FROM document_versions v WHERE v.document_id = %s "
                        "ORDER BY v.ord", (as_uuid(document_id),))
            return [_vinfo(r) for r in cur.fetchall()]

    def latest_child(self, document_id, parent_id):
        with self.db.tx() as cur:
            cur.execute(f"SELECT {_VINFO} FROM document_versions v WHERE v.document_id = %s "
                        "AND v.parent_id = %s ORDER BY v.created_at DESC, v.ord DESC LIMIT 1",
                        (as_uuid(document_id), as_uuid(parent_id)))
            row = cur.fetchone()
        return _vinfo(row) if row else None

    def set_head(self, document_id, version_id, *, owner_id):
        did, vid, oid = as_uuid(document_id), as_uuid(version_id), as_uuid(owner_id)
        if None in (did, vid, oid):
            return None
        with self.db.tx() as cur:
            cur.execute(
                f"UPDATE documents d SET head_version_id = %s, updated_at = now() "
                "WHERE d.id = %s AND d.owner_id = %s AND EXISTS (SELECT 1 FROM document_versions v "
                f"WHERE v.id = %s AND v.document_id = d.id) RETURNING {_DOC}",
                (vid, did, oid, vid))
            row = cur.fetchone()
        return _doc(row) if row else None

    def set_visibility(self, document_id, visibility, *, owner_id):
        did, oid = as_uuid(document_id), as_uuid(owner_id)
        if did is None or oid is None:
            return None
        with self.db.tx() as cur:
            cur.execute(
                f"""UPDATE documents d SET visibility = %s,
                      shared_at = CASE WHEN %s = 'school' THEN COALESCE(d.shared_at, now()) END
                    WHERE d.id = %s AND d.owner_id = %s
                      AND (%s = 'private' OR d.tenant_id IS NOT NULL)
                    RETURNING {_DOC}""",
                (visibility.value, visibility.value, did, oid, visibility.value))
            row = cur.fetchone()
            if row is None and visibility == Visibility.school:
                cur.execute("SELECT 1 FROM documents WHERE id = %s AND owner_id = %s", (did, oid))
                if cur.fetchone() is not None:
                    raise ValueError("a document without a school cannot be shared")
        return _doc(row) if row else None

    def delete(self, document_id, *, owner_id):
        did, oid = as_uuid(document_id), as_uuid(owner_id)
        if did is None or oid is None:
            return False
        with self.db.tx() as cur:   # versions cascade; copies keep living (copied_from → NULL)
            cur.execute("DELETE FROM documents WHERE id = %s AND owner_id = %s RETURNING 1",
                        (did, oid))
            return cur.fetchone() is not None

    def counts(self):
        with self.db.tx() as cur:
            cur.execute("SELECT count(*) FILTER (WHERE kind = 'lesson') AS lesson, "
                        "count(*) FILTER (WHERE kind = 'unit') AS unit, "
                        "count(*) FILTER (WHERE visibility = 'school') AS shared FROM documents")
            return {k: int(v) for k, v in cur.fetchone().items()}

