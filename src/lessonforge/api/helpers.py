"""Shared helpers for the API routers: view shaping, chunker resolution, file
responses, SSE framing."""

from __future__ import annotations

import json
from typing import Any

from fastapi import Response

from ..export import RenderedArtifact
from ..rag.chunkers import build_chunker
from ..rag.documents import (
    CHAPTER_KEY,
    CHUNK_INDEX_KEY,
    COLLECTION_KEY,
    HEADING_PATH_KEY,
    SOURCE_KEY,
    TEXT_KEY,
    Document,
)
from ..rag.loaders import split_front_matter
from ..services.chat.events import ChatEvent
from .schemas import DocumentIngestRequest

# Payload keys that carry the record's own text/source/collection or store
# internals — surfaced as dedicated fields, so they're stripped from the
# free-form "metadata" the browse UI shows.
_RESERVED_PAYLOAD_KEYS = frozenset({TEXT_KEY, SOURCE_KEY, COLLECTION_KEY})


def record_view(rec: Any, fallback_collection: str) -> dict[str, Any]:
    """Shape a stored point for the browse UI: promote text/source/collection,
    keep the rest as displayable metadata (dropping store internals like the
    leading-underscore round-trip keys)."""
    payload = rec.payload or {}
    metadata = {
        k: v
        for k, v in payload.items()
        if k not in _RESERVED_PAYLOAD_KEYS and not k.startswith("_")
    }
    return {
        "id": rec.id,
        "text": payload.get(TEXT_KEY, ""),
        "source": payload.get(SOURCE_KEY, ""),
        "collection": payload.get(COLLECTION_KEY, fallback_collection),
        "metadata": metadata,
        "space": payload.get("_space"),
    }


def resolve_document_chunker(settings: Any, req: DocumentIngestRequest):
    """Pick the chunker for a document-ingest request: an explicit ``chunker``, or
    the per-format default from ``chunking`` config; tuned by merging config
    ``params`` with the request's (request wins). ``build_chunker`` drops params a
    given chunker doesn't accept, so cross-strategy knobs are safe."""
    name = req.chunker or settings.chunking.by_format.get(req.format, settings.chunking.default)
    params = {**settings.chunking.params, **(req.params or {})}
    return build_chunker(name, **params)


def document_from_request(req: DocumentIngestRequest) -> Document:
    """Build the source Document from raw text, honoring optional Markdown
    front-matter (explicit request ``metadata`` wins over front-matter keys)."""
    text, meta, source = req.text, dict(req.metadata or {}), req.source
    if req.format == "markdown":
        front, text = split_front_matter(req.text)
        source = source or (str(front["source"]) if front.get("source") else None)
        merged = {k: v for k, v in front.items() if k not in {"id", "source"}}
        merged.update(meta)
        meta = merged
    return Document(id=source or "ui-document", text=text.strip(),
                    source=source or "ui-upload", metadata=meta)


def grounding_tree(bundle: Any) -> dict[str, Any]:
    """Shape a GroundingBundle into a compact provenance tree for the UI: collection
    → document (source) → chunk breadcrumbs (heading_path/chapter + rerank score).
    Identity and hierarchy only — NO chunk text, so it shows *what* grounded a lesson
    without quoting the material. Authoritative collections sort first."""
    collections: list[dict[str, Any]] = []
    for name, chunks in bundle.chunks.items():
        if not chunks:
            continue
        docs: dict[str, list[dict[str, Any]]] = {}
        for c in chunks:
            p = c.payload or {}
            src = str(p.get(SOURCE_KEY) or "unknown source")
            docs.setdefault(src, []).append({
                "heading_path": p.get(HEADING_PATH_KEY),
                "chapter": p.get(CHAPTER_KEY),
                "chunk_index": p.get(CHUNK_INDEX_KEY),
                "score": round(float(c.score), 3),
                "text": c.text,  # shown only on demand (expand a chunk)
            })
        collections.append({
            "name": name,
            "authoritative": name in bundle.authoritative,
            "count": sum(len(v) for v in docs.values()),
            "documents": [{"source": s, "chunks": cs} for s, cs in docs.items()],
        })
    collections.sort(key=lambda col: (not col["authoritative"], -col["count"]))
    return {
        "grounded": not bundle.is_empty,
        "has_authoritative": bundle.has_authoritative,
        "sources": list(bundle.sources),
        "collections": collections,
    }


def chunk_view(chunk: Any) -> dict[str, Any]:
    """One preview chunk for the UI: its breadcrumb, size, text, and the metadata
    that will be stored (heading_path/heading pulled out for display)."""
    md = dict(chunk.metadata)
    return {
        "heading_path": md.pop("heading_path", None),
        "heading": md.pop("heading", None),
        "chars": len(chunk.text),
        "text": chunk.text,
        "metadata": md,
    }


def file_response(art: RenderedArtifact) -> Response:
    safe = art.filename.replace('"', "").replace("\r", "").replace("\n", "")
    return Response(content=art.content, media_type=art.media_type,
                    headers={"Content-Disposition": f'attachment; filename="{safe}"'})


def sse(event: ChatEvent) -> str:
    """Encode one pipeline event as an SSE frame."""
    return f"event: {event.type}\ndata: {json.dumps(event.data, default=str)}\n\n"
