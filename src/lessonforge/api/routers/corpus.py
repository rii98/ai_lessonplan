"""Corpus management: add, browse and delete grounding records, per knowledge
space. Requires ``corpus.manage`` (today: superadmin only).

Every read is limited to the caller's READ scope, every write to their WRITE scope
(``knowledge.write``); ingestion and deletion are written to the audit log.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from ...activity import events as act
from ...audit import AuditEntry
from ...auth.principal import Perm, Principal
from ...container import Container
from ...knowledge.scope import GLOBAL_SPACE, AccessScope, valid_space
from ...rag.chunkers import CHUNKER_REGISTRY
from ...rag.documents import DOC_ID_KEY, Collection
from ...rag.ingest import IngestReport
from ...request_context import RequestContext
from ..activity import emit
from ..deps import current_principal, get_container, request_ctx, require
from ..errors import ApiError, unprocessable
from ..helpers import (
    chunk_view,
    document_from_request,
    record_view,
    resolve_document_chunker,
)
from ..schemas import CorpusDeleteRequest, CorpusIngestRequest, DocumentIngestRequest

router = APIRouter(prefix="/api/v1/corpus", tags=["corpus"],
                   dependencies=[Depends(require(Perm.CORPUS_MANAGE))])


def _read_scope(ctx: RequestContext, space: str | None) -> AccessScope:
    """The caller's read scope, optionally narrowed to one space they can read."""
    if space is None:
        return ctx.access_scope
    if not valid_space(space) or not ctx.access_scope.allows(space):
        raise ApiError(403, "perm.denied", f"no read access to space {space!r}")
    return AccessScope.of(space)


def _check_write(c: Container, p: Principal, space: str) -> None:
    if not valid_space(space):
        raise unprocessable(f"invalid knowledge space {space!r} (use 'global' or 'school:<id>')")
    if not c.platform.knowledge_policy.can_write(p, space):
        raise ApiError(403, "perm.denied", f"no write access to space {space!r}")


def _counts(c: Container, scope: AccessScope) -> dict[str, int]:
    return {col.value: c.vector_store.count(col.value, scope=scope) for col in Collection}


def _audit(c: Container, p: Principal, action: str, target: str, detail: dict[str, Any]) -> None:
    c.platform.audit.record(AuditEntry(actor=f"{p.issuer}:{p.subject}", action=action,
                                       target=target, detail=detail, principal_id=p.id))


def _ingested(c: Container, p: Principal, report: IngestReport, space: str,
              collection: Collection, scope: AccessScope) -> dict[str, object]:
    _audit(c, p, "corpus.ingest", f"{space}/{collection.value}",
           {"documents": report.documents, "chunks": report.chunks,
            "doc_ids": report.doc_ids[:50]})
    emit(c, "corpus.ingested", act.CorpusIngested(collection=collection.value, space=space,
                                                  documents=report.documents,
                                                  chunks=report.chunks), resource_type="corpus")
    return {"documents": report.documents, "chunks": report.chunks, "space": space,
            "sources": sorted(report.sources), "counts": _counts(c, scope)}


@router.get("/spaces")
def corpus_spaces(c: Container = Depends(get_container),
                  ctx: RequestContext = Depends(request_ctx)) -> dict[str, object]:
    """The knowledge spaces the caller can read, and which they can write."""
    assert ctx.principal is not None
    policy = c.platform.knowledge_policy
    spaces = [s for s in c.platform.knowledge_registry.spaces() if ctx.access_scope.allows(s.id)]
    return {"spaces": [{"id": s.id, "kind": s.kind, "name": s.name, "status": s.status,
                        "writable": policy.can_write(ctx.principal, s.id)} for s in spaces],
            "default": GLOBAL_SPACE}


@router.get("/overview")
def corpus_overview(space: str | None = Query(None, max_length=80),
                    c: Container = Depends(get_container),
                    ctx: RequestContext = Depends(request_ctx)) -> dict[str, object]:
    """Every collection with its description and live count (within ``space``, or
    everything the caller can read)."""
    scope = _read_scope(ctx, space)
    return {"collections": [{"name": col.value, "description": col.description,
                             "count": c.vector_store.count(col.value, scope=scope)}
                            for col in Collection]}


@router.get("/collections/{name}/records")
def corpus_records(name: Collection, space: str | None = Query(None, max_length=80),
                   limit: int = Query(default=25, ge=1, le=100),
                   cursor: str | None = Query(default=None, max_length=256,
                                              description="opaque next-page token"),
                   c: Container = Depends(get_container),
                   ctx: RequestContext = Depends(request_ctx)) -> dict[str, object]:
    """Page through one collection's records (read-only browse)."""
    records, nxt = c.vector_store.scroll(name.value, limit=limit, offset=cursor,
                                         scope=_read_scope(ctx, space))
    return {"items": [record_view(r, name.value) for r in records], "next_cursor": nxt}


@router.get("/sources")
def corpus_sources(space: str | None = Query(None, max_length=80),
                   collection: Collection | None = None,
                   limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0, le=100_000),
                   c: Container = Depends(get_container),
                   ctx: RequestContext = Depends(request_ctx)) -> dict[str, object]:
    """Ingested documents (the corpus record book), newest first."""
    scope = _read_scope(ctx, space)
    spaces = None if scope.unrestricted else sorted(scope.spaces)
    rows = c.platform.knowledge_registry.list_sources(
        spaces=spaces, collection=collection.value if collection else None,
        limit=limit + 1, offset=offset)
    return {"items": [r.view() for r in rows[:limit]],
            "next_offset": offset + limit if len(rows) > limit else None}


@router.post("/ingest")
def corpus_ingest(req: CorpusIngestRequest, c: Container = Depends(get_container),
                  p: Principal = Depends(current_principal),
                  ctx: RequestContext = Depends(request_ctx)) -> dict[str, object]:
    """Add or update grounding records in a space. Idempotent per space."""
    _check_write(c, p, req.space)
    try:
        report = c.ingestor.ingest_records(req.collection, req.records, source_label="ui",
                                           space=req.space, ingested_by=p.id)
    except ValueError as exc:  # missing 'text', unknown subject, …
        raise unprocessable(str(exc)) from exc
    return _ingested(c, p, report, req.space, req.collection, ctx.access_scope)


@router.get("/chunkers")
def corpus_chunkers(c: Container = Depends(get_container)) -> dict[str, object]:
    """The chunkers available for document ingest and the per-format defaults."""
    return {"chunkers": sorted(CHUNKER_REGISTRY), "default": c.settings.chunking.default,
            "by_format": c.settings.chunking.by_format, "params": c.settings.chunking.params}


@router.post("/preview")
def corpus_preview(req: DocumentIngestRequest,
                   c: Container = Depends(get_container)) -> dict[str, object]:
    """Dry-run: split a document and return the chunks WITHOUT storing anything."""
    try:
        chunker = resolve_document_chunker(c.settings, req)
        doc = document_from_request(req)
        chunks = chunker.chunk(doc.text, collection=req.collection, source=doc.source,
                               metadata=doc.metadata)
    except ValueError as exc:
        raise unprocessable(str(exc)) from exc
    limit = 60
    return {"total": len(chunks), "truncated": len(chunks) > limit,
            "chunks": [chunk_view(ch) for ch in chunks[:limit]]}


@router.post("/ingest/document")
def corpus_ingest_document(req: DocumentIngestRequest, c: Container = Depends(get_container),
                           p: Principal = Depends(current_principal),
                           ctx: RequestContext = Depends(request_ctx)) -> dict[str, object]:
    """Ingest one whole document with a structure-aware chunker into a space."""
    _check_write(c, p, req.space)
    try:
        chunker = resolve_document_chunker(c.settings, req)
        doc = document_from_request(req)
        report = c.ingestor.ingest_documents(req.collection, [doc], chunker=chunker,
                                             space=req.space, ingested_by=p.id)
    except ValueError as exc:
        raise unprocessable(str(exc)) from exc
    return _ingested(c, p, report, req.space, req.collection, ctx.access_scope)


@router.post("/collections/{name}/delete")
def corpus_delete(name: Collection, req: CorpusDeleteRequest,
                  c: Container = Depends(get_container),
                  p: Principal = Depends(current_principal),
                  ctx: RequestContext = Depends(request_ctx)) -> dict[str, object]:
    """Delete records by id — only those in spaces the caller may write."""
    write = c.platform.knowledge_policy.write_scope(p)
    deleted = c.vector_store.delete(name.value, req.ids, scope=write)
    _audit(c, p, "corpus.delete", name.value, {"ids": req.ids[:100], "requested": len(req.ids)})
    return {"deleted": deleted, "count": c.vector_store.count(name.value, scope=ctx.access_scope)}


@router.delete("/sources/{collection}/{doc_id:path}")
def corpus_delete_source(collection: Collection, doc_id: str,
                         space: str = Query(GLOBAL_SPACE, max_length=80),
                         c: Container = Depends(get_container),
                         p: Principal = Depends(current_principal)) -> dict[str, object]:
    """Remove one ingested document: its chunks and its record-book row."""
    _check_write(c, p, space)
    c.vector_store.delete_where(collection.value, {DOC_ID_KEY: doc_id},
                                scope=AccessScope.of(space))
    removed = c.platform.knowledge_registry.remove_source(space, collection.value, doc_id)
    _audit(c, p, "corpus.delete_source", f"{space}/{collection.value}/{doc_id}",
           {"registered": removed is not None})
    return {"deleted": True, "registered": removed is not None}
