"""FastAPI application factory."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Response
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError, model_validator

from ..container import Container
from ..domain.artifacts import Quiz, Slides, Worksheet
from ..domain.chat import (
    ChatMessageRequest,
    Conversation,
    CreateConversationRequest,
    UpdateConversationRequest,
)
from ..domain.document import DocumentVersion, StoredDocument
from ..domain.ldd import IntakeRequest, LessonDesignDocument, NormalizedBrief
from ..domain.profile import TeacherProfile
from ..domain.refine import RefineRequest, RefineResult
from ..domain.rubric import Critique
from ..domain.sections import LDD_SECTIONS, WHOLE_DOCUMENT
from ..domain.unit import UnitDesignDocument, UnitPlan, UnitRequest
from ..export import ArtifactKind, RenderedArtifact, formats_for
from ..rag.chunkers import CHUNKER_REGISTRY, build_chunker
from ..rag.documents import (
    CHAPTER_KEY,
    CHUNK_INDEX_KEY,
    COLLECTION_KEY,
    HEADING_PATH_KEY,
    SOURCE_KEY,
    TEXT_KEY,
    Collection,
    Document,
)
from ..rag.loaders import split_front_matter
from ..services.artifact_generation import generatable_kinds
from ..services.chat.pipeline import ChatEvent
from ..services.unit_coherence import UnitCoherence
from .deps import get_container

# Maps a targeted artifact kind to the IR model its generate/export endpoints use.
_ARTIFACT_MODELS: dict[ArtifactKind, type] = {
    ArtifactKind.quiz: Quiz,
    ArtifactKind.worksheet: Worksheet,
    ArtifactKind.slides: Slides,
}

_STATIC_DIR = Path(__file__).parent / "static"
_UI_INDEX = _STATIC_DIR / "index.html"
_UI_CORPUS = _STATIC_DIR / "corpus.html"
_UI_CHAT = _STATIC_DIR / "chat.html"
_UI_UNITS = _STATIC_DIR / "units.html"
_UI_LIBRARY = _STATIC_DIR / "library.html"
# Vendored, version-pinned browser libraries (markdown-it, KaTeX, DOMPurify,
# highlight.js) so rich rendering works fully offline — no runtime CDN.
_VENDOR_DIR = _STATIC_DIR / "vendor"

# Payload keys that carry the record's own text/source/collection or store
# internals — surfaced as dedicated fields, so they're stripped from the
# free-form "metadata" the browse UI shows.
_RESERVED_PAYLOAD_KEYS = frozenset({TEXT_KEY, SOURCE_KEY, COLLECTION_KEY})


class ValidationResult(BaseModel):
    """Result of validating an edited LDD before export. Returned with HTTP 200
    even when invalid, so the editor UI can render field-level errors inline
    rather than treating a normal editing state as a request failure."""

    valid: bool
    errors: list[dict[str, str]] = Field(default_factory=list)


def _format_errors(exc: ValidationError) -> list[dict[str, str]]:
    """Flatten pydantic errors into ``{loc, msg}`` the UI can pin to a field."""
    out: list[dict[str, str]] = []
    for e in exc.errors():
        loc = ".".join(str(p) for p in e["loc"])
        out.append({"loc": loc, "msg": e["msg"]})
    return out


class GenerateRequest(BaseModel):
    """A generation request. ``topic`` may be omitted when ``existing_plan`` is
    supplied — intake extracts it from the pasted plan (US-3)."""

    topic: str | None = Field(default=None, examples=["Components of Environment: Biotic and Abiotic"])
    grade: int | None = Field(default=None, ge=1, le=12, examples=[6])
    subject: str | None = Field(default=None, examples=["Science"])
    duration_min: Literal[30, 45, 60] | None = None
    language: Literal["en", "ne", "en-ne"] | None = None
    framework: Literal["5E", "gradual_release", "inquiry"] | None = None
    existing_plan: str | None = None

    @model_validator(mode="after")
    def _need_topic_or_plan(self) -> GenerateRequest:
        if not self.topic and not self.existing_plan:
            raise ValueError("provide `topic`, or `existing_plan` for intake to parse")
        return self


class RefineLessonRequest(BaseModel):
    """Reprompt one section of a lesson. ``ldd`` is the current (possibly already
    hand-edited) lesson; ``target`` names the section to improve (or ``"*"`` for
    the whole lesson); ``instruction`` is the teacher's free-text intent."""

    ldd: LessonDesignDocument
    target: str = Field(examples=["engagement_hook"])
    instruction: str = Field(min_length=1, examples=["make the hook about the local river"])


class SaveLessonRequest(GenerateRequest):
    """Generate a lesson AND persist it as a new document with its first version.
    Adds ownership/title to the generation request; everything else is inherited."""

    owner_id: str = "default"
    title: str | None = None


class DocumentRefineRequest(BaseModel):
    """Refine the current head of a stored lesson. When ``commit`` is true and the
    refine succeeds, the accepted candidate is appended as a new ``refine`` version
    (with its instruction + diff recorded); otherwise it is only proposed."""

    target: str = Field(examples=["engagement_hook"])
    instruction: str = Field(min_length=1, examples=["make the hook about the local river"])
    commit: bool = False


class DocumentView(BaseModel):
    """A stored document plus the LDD content at the requested version."""

    document: StoredDocument
    ldd: LessonDesignDocument


class SavedLesson(BaseModel):
    """The result of persisting a generated lesson."""

    document: StoredDocument
    version_id: str
    ldd: LessonDesignDocument


class DocumentRefineResponse(BaseModel):
    """A refine outcome on a stored document; ``version_id`` is set only when the
    refine was committed as a new version."""

    result: RefineResult
    version_id: str | None = None


class GenerateUnitRequest(UnitRequest):
    """Plan-and-expand a multi-day unit AND persist it. Adds ownership/title to the
    unit request. When ``plan`` is supplied (a teacher-edited spine from
    ``/units/plan``) the planner is skipped and that exact arc is expanded, so edits
    to the spine actually drive generation."""

    owner_id: str = "default"
    title: str | None = None
    plan: UnitPlan | None = None


class UnitView(BaseModel):
    """A stored unit plus its content at the requested version."""

    document: StoredDocument
    unit: UnitDesignDocument


class SavedUnit(BaseModel):
    """The result of persisting a generated (or regenerated) unit."""

    document: StoredDocument
    version_id: str
    unit: UnitDesignDocument


class CorpusIngestRequest(BaseModel):
    """Add/update grounding records in a collection. Each record is a JSONL-style
    object (``text`` required; ``grade``/``subject``/``standard``/``language``/
    ``topic``/``source`` optional). Idempotent: re-submitting the same text
    updates the existing point instead of duplicating it."""

    collection: Collection
    records: list[dict[str, Any]] = Field(..., min_length=1)


class CorpusDeleteRequest(BaseModel):
    ids: list[str] = Field(..., min_length=1)


class GroundingPreviewRequest(BaseModel):
    """Inspect what grounding a topic retrieves — the same deterministic retrieval
    generation uses — so the UI can show WHICH chunks (and their heading hierarchy)
    a lesson is grounded in, without re-running the LLM or quoting the text."""

    topic: str = Field(..., min_length=1, examples=["Electricity and Magnetism"])
    grade: int | None = Field(default=None, ge=1, le=12)
    subject: str | None = None
    framework: str | None = None


class DocumentIngestRequest(BaseModel):
    """Ingest ONE whole document (a book chapter, an exemplar lesson) as raw text,
    split by a structure-aware chunker. Unlike the JSONL path (one record → one
    chunk), the chunker slices the document and — for ``markdown`` — preserves the
    header hierarchy as ``heading_path`` metadata on every chunk.

    ``chunker`` overrides the per-format default; ``params`` tune it
    (``max_chars``, ``min_chars``, ``split_levels``, ``prepend_breadcrumb``,
    ``include_heading``). Optional ``---`` YAML front-matter in ``text`` is merged
    into ``metadata`` (explicit ``metadata`` wins)."""

    collection: Collection
    text: str = Field(..., min_length=1)
    format: str = "markdown"
    source: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    chunker: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)


def _record_view(rec: Any, fallback_collection: str) -> dict[str, Any]:
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
    }


def _resolve_document_chunker(settings: Any, req: DocumentIngestRequest):
    """Pick the chunker for a document-ingest request: an explicit ``chunker``, or
    the per-format default from ``chunking`` config; tuned by merging config
    ``params`` with the request's (request wins). ``build_chunker`` drops params a
    given chunker doesn't accept, so cross-strategy knobs are safe."""
    name = req.chunker or settings.chunking.by_format.get(req.format, settings.chunking.default)
    params = {**settings.chunking.params, **(req.params or {})}
    return build_chunker(name, **params)


def _document_from_request(req: DocumentIngestRequest) -> Document:
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


def _grounding_tree(bundle: Any) -> dict[str, Any]:
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


def _chunk_view(chunk: Any) -> dict[str, Any]:
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


def create_app() -> FastAPI:
    app = FastAPI(
        title="LessonForge — AI Lesson Plan Creator",
        version="0.1.0",
        summary="AI that thinks like an experienced teacher.",
    )

    # Serve the whole static directory (shared app.css/app.js, plus the vendored,
    # version-pinned browser libraries under /static/vendor). No build step.
    if _STATIC_DIR.is_dir():
        app.mount(
            "/static",
            StaticFiles(directory=_STATIC_DIR),
            name="static",
        )

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def index() -> str:
        """The edit-before-export web editor — a single self-contained page
        (low-bandwidth first, no build step, one request)."""
        if _UI_INDEX.exists():
            return _UI_INDEX.read_text(encoding="utf-8")
        return "<h1>LessonForge</h1><p>UI asset missing. See /docs for the API.</p>"

    @app.get("/units", response_class=HTMLResponse, include_in_schema=False)
    def units_page() -> str:
        """The multi-day unit planner: plan the arc, expand each day into a full
        lesson, regenerate a single day, and export the whole unit."""
        if _UI_UNITS.exists():
            return _UI_UNITS.read_text(encoding="utf-8")
        return "<h1>Unit planner</h1><p>UI asset missing. See /docs for the API.</p>"

    @app.get("/library", response_class=HTMLResponse, include_in_schema=False)
    def library_page() -> str:
        """Saved lessons and units with version history — revisit, undo/redo,
        export, delete."""
        if _UI_LIBRARY.exists():
            return _UI_LIBRARY.read_text(encoding="utf-8")
        return "<h1>Library</h1><p>UI asset missing. See /docs for the API.</p>"

    @app.get("/health", tags=["ops"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/providers", tags=["ops"])
    def provider_health(c: Container = Depends(get_container)) -> dict[str, object]:
        """Reports which configured backend is wired for each component and
        whether the networked ones are reachable."""
        s = c.settings
        return {
            "llm": {"provider": s.llm.provider, "model": s.llm.model, "reachable": c.llm.health()},
            "embedding": {"provider": s.embedding.provider, "model": s.embedding.model},
            "reranker": {"provider": s.reranker.provider, "model": s.reranker.model},
            "vector_store": {
                "provider": s.vector_store.provider,
                "reachable": c.vector_store.health(),
            },
        }

    @app.post("/lessons/generate", response_model=LessonDesignDocument, tags=["lessons"])
    def generate_lesson(
        req: GenerateRequest, c: Container = Depends(get_container)
    ) -> LessonDesignDocument:
        """Full pipeline: intake → enrichment → critique & revise → validated LDD.
        The teacher's stored profile fills unset fields and flavours the lesson."""
        profile = c.profile_store.load()
        try:
            return c.pipeline.run(IntakeRequest(**req.model_dump()), profile=profile)
        except ValueError as exc:  # intake couldn't determine topic/grade
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/lessons/intake", response_model=NormalizedBrief, tags=["lessons"])
    def intake_lesson(
        req: GenerateRequest, c: Container = Depends(get_container)
    ) -> NormalizedBrief:
        """US-3 preview: normalize a raw request / pasted plan into a brief,
        without generating — so a teacher can confirm what was parsed. Profile
        defaults are applied here too, so the preview matches what generate sees."""
        profile = c.profile_store.load()
        try:
            request = profile.apply_defaults(IntakeRequest(**req.model_dump()))
            return profile.personalize(c.intake.normalize(request))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/lessons/critique", response_model=Critique, tags=["lessons"])
    def critique_lesson(
        ldd: LessonDesignDocument, c: Container = Depends(get_container)
    ) -> Critique:
        """Score an already-generated LDD against the anti-generic rubric."""
        return c.pipeline.critique(ldd)

    @app.get("/lessons/refine/targets", tags=["lessons"])
    def refine_targets() -> dict[str, object]:
        """The reprompt-able sections and their edit blast radius, for the UI to
        render an "improve this" control per section. ``coupled`` lists the
        neighbours a change may cascade into; leaf sections are zero-risk."""
        return {
            "sections": [
                {"target": s.name, "coupled": list(s.coupled), "leaf": s.is_leaf}
                for s in LDD_SECTIONS.values()
            ],
            "whole_document": WHOLE_DOCUMENT,
        }

    @app.post("/lessons/refine", response_model=RefineResult, tags=["lessons"])
    def refine_lesson(
        req: RefineLessonRequest, c: Container = Depends(get_container)
    ) -> RefineResult:
        """Reprompt one section (or the whole lesson) and PROPOSE an improved,
        re-validated LDD — the teacher accepts or rejects the returned candidate.
        A change that would break a structural guardrail is repaired via a bounded
        cascade or rejected with the reason; it is never silently forced through."""
        try:
            return c.refiner.refine(
                req.ldd, RefineRequest(target=req.target, instruction=req.instruction)
            )
        except ValueError as exc:  # unknown target
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/lessons/validate", response_model=ValidationResult, tags=["lessons"])
    def validate_ldd(payload: dict[str, Any] = Body(...)) -> ValidationResult:
        """Validate an edited LDD against the anti-generic guardrails WITHOUT
        exporting. The editor UI calls this after a teacher's edits so guardrail
        violations (missing hook, an objective not assessed, …) surface as
        field-level messages before they try to export."""
        try:
            LessonDesignDocument.model_validate(payload)
        except ValidationError as exc:
            return ValidationResult(valid=False, errors=_format_errors(exc))
        return ValidationResult(valid=True)

    @app.post("/grounding/preview", tags=["grounding"])
    def grounding_preview(
        req: GroundingPreviewRequest, c: Container = Depends(get_container)
    ) -> dict[str, Any]:
        """Show WHICH chunks — and their heading hierarchy — a lesson on this topic
        grounds in. Runs the SAME deterministic retrieval generation uses (no LLM,
        no chunk text). A live view: it reflects the corpus as it is now."""
        empty = {"grounded": False, "has_authoritative": False, "sources": [], "collections": []}
        grounding = getattr(c, "grounding", None)
        if grounding is None:
            return empty
        # mirror the generator's query so the preview matches what actually grounds.
        query = (
            f"{req.topic} grade {req.grade} {req.subject}"
            if req.grade and req.subject else req.topic
        )
        try:
            bundle = grounding.ground(
                query=query, grade=req.grade, subject=req.subject, framework=req.framework
            )
        except Exception:
            return empty
        return _grounding_tree(bundle)

    # ── documents: persisted lessons with version history (revisit/edit/undo) ─
    @app.post("/documents/lessons", response_model=SavedLesson, tags=["documents"])
    def create_lesson_document(
        req: SaveLessonRequest, c: Container = Depends(get_container)
    ) -> SavedLesson:
        """Generate a lesson through the full pipeline and persist it as a new
        document with its first (``generate``) version — the entry point for work a
        teacher can come back to, edit, and undo."""
        profile = c.profile_store.load()
        gen = req.model_dump(exclude={"owner_id", "title"})
        try:
            ldd = c.pipeline.run(IntakeRequest(**gen), profile=profile)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        doc, version = c.get_document_service().save_lesson(
            ldd, owner_id=req.owner_id, title=req.title
        )
        return SavedLesson(document=doc, version_id=version.id, ldd=ldd)

    @app.get("/documents", response_model=list[StoredDocument], tags=["documents"])
    def list_documents(
        owner_id: str = Query("default"), c: Container = Depends(get_container)
    ) -> list[StoredDocument]:
        """A teacher's saved documents, most-recently-updated first."""
        return c.get_document_store().list(owner_id)

    @app.get("/documents/{document_id}", response_model=DocumentView, tags=["documents"])
    def get_document(
        document_id: str,
        version: str | None = Query(None, description="a specific version id; default = head"),
        c: Container = Depends(get_container),
    ) -> DocumentView:
        """The document header plus its content at ``version`` (default: the head)."""
        svc = c.get_document_service()
        doc = svc.store.get(document_id)
        if doc is None:
            raise HTTPException(status_code=404, detail="document not found")
        ldd = (
            svc.get_lesson_version(document_id, version) if version
            else svc.head_lesson(document_id)
        )
        if ldd is None:
            raise HTTPException(status_code=404, detail="version not found")
        return DocumentView(document=doc, ldd=ldd)

    @app.get(
        "/documents/{document_id}/versions",
        response_model=list[DocumentVersion], tags=["documents"],
    )
    def document_versions(
        document_id: str, c: Container = Depends(get_container)
    ) -> list[DocumentVersion]:
        """The full edit history (oldest first) — the audit trail of what produced
        each state (origin, instruction, diff, sources, scores)."""
        svc = c.get_document_service()
        if svc.store.get(document_id) is None:
            raise HTTPException(status_code=404, detail="document not found")
        return svc.store.versions(document_id)

    @app.post(
        "/documents/{document_id}/refine",
        response_model=DocumentRefineResponse, tags=["documents"],
    )
    def refine_document(
        document_id: str, req: DocumentRefineRequest, c: Container = Depends(get_container)
    ) -> DocumentRefineResponse:
        """Refine the head lesson. Proposes by default; with ``commit=true`` an
        accepted refine is appended as a new version."""
        svc = c.get_document_service()
        head = svc.head_lesson(document_id)
        if head is None:
            raise HTTPException(status_code=404, detail="document not found")
        try:
            result = c.refiner.refine(
                head, RefineRequest(target=req.target, instruction=req.instruction)
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        version_id: str | None = None
        if req.commit and result.ok:
            version = svc.commit_refine(document_id, result)
            version_id = version.id if version else None
        return DocumentRefineResponse(result=result, version_id=version_id)

    @app.post("/documents/{document_id}/manual", response_model=SavedLesson, tags=["documents"])
    def commit_manual_edit(
        document_id: str, ldd: LessonDesignDocument, c: Container = Depends(get_container)
    ) -> SavedLesson:
        """Persist a teacher's direct edit of a stored lesson (from the full editor)
        as a new ``manual`` version on top of the head."""
        svc = c.get_document_service()
        if svc.store.get(document_id) is None:
            raise HTTPException(status_code=404, detail="document not found")
        version = svc.commit_manual(document_id, ldd)
        if version is None:
            raise HTTPException(status_code=404, detail="document not found")
        return SavedLesson(document=svc.store.get(document_id), version_id=version.id, ldd=ldd)

    @app.post("/documents/{document_id}/undo", response_model=StoredDocument, tags=["documents"])
    def undo_document(
        document_id: str, c: Container = Depends(get_container)
    ) -> StoredDocument:
        """Move the head back to the previous version. 409 when already at the root."""
        moved = c.get_document_service().undo(document_id)
        if moved is None:
            raise HTTPException(status_code=409, detail="nothing to undo")
        return moved

    @app.post("/documents/{document_id}/redo", response_model=StoredDocument, tags=["documents"])
    def redo_document(
        document_id: str, c: Container = Depends(get_container)
    ) -> StoredDocument:
        """Move the head forward again after an undo. 409 when there is no child."""
        moved = c.get_document_service().redo(document_id)
        if moved is None:
            raise HTTPException(status_code=409, detail="nothing to redo")
        return moved

    @app.delete("/documents/{document_id}", tags=["documents"])
    def delete_document(
        document_id: str, c: Container = Depends(get_container)
    ) -> dict[str, bool]:
        """Delete a document and its entire history."""
        return {"deleted": c.get_document_store().delete(document_id)}

    # ── units: plan-and-expand multi-day units ───────────────────────────────
    def _unit_request_from(unit: UnitDesignDocument) -> UnitRequest:
        """Reconstruct the generation request from a stored unit, so a single day
        can be regenerated against the same framing."""
        d0 = unit.days[0]
        return UnitRequest(
            topic=unit.title, grade=unit.curriculum_ref.grade,
            subject=unit.curriculum_ref.subject, num_days=len(unit.days),
            duration_min=d0.duration_min, language=d0.language, framework=d0.framework,
        )

    @app.post("/units/plan", response_model=UnitPlan, tags=["units"])
    def plan_unit(req: UnitRequest, c: Container = Depends(get_container)) -> UnitPlan:
        """Produce the unit spine (the arc) for review BEFORE the expensive
        per-day expansion — the gate a teacher edits or regenerates cheaply."""
        profile = c.profile_store.load()
        try:
            return c.unit_planner.plan(req, profile=profile)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/units/generate", response_model=SavedUnit, tags=["units"])
    def generate_unit(
        req: GenerateUnitRequest, c: Container = Depends(get_container)
    ) -> SavedUnit:
        """Plan and expand a full multi-day unit, then persist it with version
        history (revisit/regenerate/undo like any document)."""
        profile = c.profile_store.load()
        unit_req = UnitRequest(**req.model_dump(exclude={"owner_id", "title", "plan"}))
        try:
            unit = (
                c.unit_generator.expand(req.plan, unit_req, profile=profile)
                if req.plan is not None
                else c.unit_generator.generate(unit_req, profile=profile)
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        doc, version = c.get_document_service().save_unit(
            unit, owner_id=req.owner_id, title=req.title
        )
        return SavedUnit(document=doc, version_id=version.id, unit=unit)

    @app.get("/units/{document_id}", response_model=UnitView, tags=["units"])
    def get_unit(
        document_id: str,
        version: str | None = Query(None, description="a version id; default = head"),
        c: Container = Depends(get_container),
    ) -> UnitView:
        """The unit header plus its content at ``version`` (default: the head)."""
        svc = c.get_document_service()
        doc = svc.store.get(document_id)
        if doc is None:
            raise HTTPException(status_code=404, detail="unit not found")
        unit = (
            svc.get_unit_version(document_id, version) if version
            else svc.head_unit(document_id)
        )
        if unit is None:
            raise HTTPException(status_code=404, detail="version not found")
        return UnitView(document=doc, unit=unit)

    @app.post(
        "/units/{document_id}/days/{day}/regenerate",
        response_model=SavedUnit, tags=["units"],
    )
    def regenerate_unit_day(
        document_id: str, day: int, c: Container = Depends(get_container)
    ) -> SavedUnit:
        """Rebuild ONE day against the same arc — the other days are untouched —
        and commit the result as a new version."""
        svc = c.get_document_service()
        unit = svc.head_unit(document_id)
        if unit is None:
            raise HTTPException(status_code=404, detail="unit not found")
        try:
            updated = c.unit_generator.regenerate_day(
                unit, day, _unit_request_from(unit), profile=c.profile_store.load()
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        version = svc.commit_unit(
            document_id, updated, instruction=f"regenerated day {day}"
        )
        return SavedUnit(
            document=svc.store.get(document_id), version_id=version.id, unit=updated
        )

    @app.put(
        "/units/{document_id}/days/{day}",
        response_model=SavedUnit, tags=["units"],
    )
    def edit_unit_day(
        document_id: str,
        day: int,
        ldd: LessonDesignDocument,
        c: Container = Depends(get_container),
    ) -> SavedUnit:
        """Replace ONE day's lesson with a teacher-edited LDD (from the full lesson
        editor), re-run coherence over the whole unit, and commit it as a new manual
        version — the manual-edit counterpart to regenerate."""
        svc = c.get_document_service()
        unit = svc.head_unit(document_id)
        if unit is None:
            raise HTTPException(status_code=404, detail="unit not found")
        if not 1 <= day <= len(unit.days):
            raise HTTPException(
                status_code=422, detail=f"day {day} is out of range 1..{len(unit.days)}"
            )
        new_days = list(unit.days)
        new_days[day - 1] = ldd
        try:
            # revalidate so cross-day invariants (grade/subject match, day count)
            # reject a bad edit instead of persisting a broken unit.
            candidate = UnitDesignDocument.model_validate(
                unit.model_copy(update={"days": new_days}).model_dump(mode="json")
            )
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=_format_errors(exc)) from exc
        updated = UnitCoherence().apply(candidate)
        version = svc.commit_unit(document_id, updated, instruction=f"edited day {day}")
        return SavedUnit(
            document=svc.store.get(document_id), version_id=version.id, unit=updated
        )

    @app.post("/units/{document_id}/export", tags=["units"])
    def export_unit(
        document_id: str, c: Container = Depends(get_container)
    ) -> Response:
        """Download the whole unit as a zip of per-day lesson plans."""
        unit = c.get_document_service().head_unit(document_id)
        if unit is None:
            raise HTTPException(status_code=404, detail="unit not found")
        return _file_response(c.exporter.unit_zip(unit))

    # ── teacher profile: stored preferences injected into every build ────────
    @app.get("/profile", response_model=TeacherProfile, tags=["profile"])
    def get_profile(c: Container = Depends(get_container)) -> TeacherProfile:
        """The current teacher's stored preferences (single-teacher v1)."""
        return c.profile_store.load()

    @app.put("/profile", response_model=TeacherProfile, tags=["profile"])
    def put_profile(
        profile: TeacherProfile, c: Container = Depends(get_container)
    ) -> TeacherProfile:
        """Save the teacher's preferences. They take effect on the next generate."""
        return c.profile_store.save(profile)

    # ── export: LDD → downloadable artifacts ─────────────────────────────────
    def _file_response(art: RenderedArtifact) -> Response:
        return Response(
            content=art.content,
            media_type=art.media_type,
            headers={"Content-Disposition": f'attachment; filename="{art.filename}"'},
        )

    @app.get("/export/manifest", tags=["export"])
    def export_manifest(c: Container = Depends(get_container)) -> dict[str, object]:
        """What the export subsystem can produce and in which formats."""
        return c.exporter.manifest()

    @app.post("/lessons/export/{kind}", tags=["export"])
    def export_artifact(
        kind: ArtifactKind,
        ldd: LessonDesignDocument,
        c: Container = Depends(get_container),
        fmt: str | None = Query(default=None, description="Override the configured format"),
    ) -> Response:
        """Render one artifact from an already-generated LDD."""
        try:
            art = c.exporter.render(kind, ldd, fmt=fmt)
        except ValueError as exc:  # unknown/unsupported format
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return _file_response(art)

    @app.post("/lessons/export/bundle/zip", tags=["export"])
    def export_bundle(
        ldd: LessonDesignDocument, c: Container = Depends(get_container)
    ) -> Response:
        """One-click zip of every configured artifact."""
        return _file_response(c.exporter.bundle(ldd))

    # ── targeted artifacts: generate ONE artifact without a whole lesson ──────
    def _generatable(kind: ArtifactKind) -> None:
        if kind not in _ARTIFACT_MODELS or kind not in generatable_kinds():
            raise HTTPException(
                status_code=422,
                detail=(f"{kind.value!r} is not generatable on its own. "
                        f"Generatable: {[k.value for k in generatable_kinds()]}."),
            )

    @app.get("/artifacts/manifest", tags=["artifacts"])
    def artifacts_manifest() -> dict[str, object]:
        """Which artifacts can be generated standalone, and in which export formats
        — what the 'generate just a…' UI renders its controls from."""
        return {
            "generatable": [
                {"kind": k.value, "formats": formats_for(k)}
                for k in generatable_kinds()
            ]
        }

    @app.post("/artifacts/{kind}/generate", tags=["artifacts"])
    def generate_artifact(
        kind: ArtifactKind,
        req: GenerateRequest,
        c: Container = Depends(get_container),
    ):
        """Generate a single artifact (quiz/worksheet/slides) straight from a brief
        — no full lesson built. Returns the editable IR so the teacher can tweak it
        before exporting, symmetric with the lesson edit-before-export flow. The
        teacher's profile fills unset fields; grounding uses the ``reference`` book
        when the topic is covered there."""
        _generatable(kind)
        profile = c.profile_store.load()
        try:
            request = profile.apply_defaults(IntakeRequest(**req.model_dump()))
            brief = profile.personalize(c.intake.normalize(request))
        except ValueError as exc:  # intake couldn't determine topic/grade
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        try:
            return c.artifact_generator(kind).generate(brief)
        except ValueError as exc:  # could not assemble a valid artifact
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/artifacts/{kind}/export", tags=["artifacts"])
    def export_generated_artifact(
        kind: ArtifactKind,
        payload: dict[str, Any] = Body(...),
        c: Container = Depends(get_container),
        fmt: str | None = Query(default=None, description="Override the configured format"),
    ) -> Response:
        """Render a standalone artifact IR (as returned by generate, possibly
        hand-edited) into a downloadable file."""
        _generatable(kind)
        try:
            ir = _ARTIFACT_MODELS[kind].model_validate(payload)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=_format_errors(exc)) from exc
        try:
            art = c.exporter.render_artifact(kind, ir, fmt=fmt)
        except ValueError as exc:  # unknown/unsupported format
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return _file_response(art)

    @app.get("/artifacts/{kind}/refine/targets", tags=["artifacts"])
    def artifact_refine_targets(kind: ArtifactKind) -> dict[str, object]:
        """The reprompt-able parts of an artifact and their edit blast radius — what
        the UI renders its per-part "✨ Improve" controls from."""
        _generatable(kind)
        from ..domain.artifact_sections import ARTIFACT_SECTIONS

        return {
            "sections": [
                {"target": s.name, "coupled": list(s.coupled), "leaf": s.is_leaf,
                 "grounded": s.grounded}
                for s in ARTIFACT_SECTIONS[kind.value].values()
            ],
            "whole_document": "*",
        }

    @app.post("/artifacts/{kind}/refine", tags=["artifacts"])
    def refine_artifact(
        kind: ArtifactKind,
        payload: dict[str, Any] = Body(...),
        c: Container = Depends(get_container),
    ):
        """Reprompt one part of an artifact (or "*" for the whole thing) and PROPOSE
        an improved, re-validated IR — the teacher accepts or rejects. A content
        edit re-grounds in the reference book and merges the new sources; a change
        that would break a guardrail is cascade-repaired or rejected with reason."""
        _generatable(kind)
        target = str(payload.get("target", "")).strip()
        instruction = str(payload.get("instruction", "")).strip()
        if not target or not instruction:
            raise HTTPException(status_code=422, detail="`target` and `instruction` are required")
        try:
            ir = _ARTIFACT_MODELS[kind].model_validate(payload.get("artifact", {}))
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=_format_errors(exc)) from exc
        try:
            result = c.artifact_refiner(kind).refine(ir, target, instruction)
        except ValueError as exc:  # unknown target
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {
            "ok": result.ok,
            "target": result.target,
            "instruction": result.instruction,
            "candidate": result.candidate.model_dump() if result.candidate else None,
            "diff": result.diff,
            "cascaded": result.cascaded,
            "errors": result.errors,
            "notes": result.notes,
        }

    # ── corpus manager: grow & curate the grounding knowledge base ───────────
    @app.get("/corpus", response_class=HTMLResponse, include_in_schema=False)
    def corpus_page() -> str:
        """Self-contained page to add, browse, and delete grounding records."""
        if _UI_CORPUS.exists():
            return _UI_CORPUS.read_text(encoding="utf-8")
        return "<h1>Corpus manager</h1><p>UI asset missing. See /docs for the API.</p>"

    @app.get("/corpus/overview", tags=["corpus"])
    def corpus_overview(c: Container = Depends(get_container)) -> dict[str, object]:
        """Every collection with its curator-facing description and live count —
        what the UI's 'what's in the knowledge base' panel renders."""
        return {
            "collections": [
                {
                    "name": col.value,
                    "description": col.description,
                    "count": c.vector_store.count(col.value),
                }
                for col in Collection
            ]
        }

    @app.get("/corpus/collections/{name}/records", tags=["corpus"])
    def corpus_records(
        name: Collection,
        c: Container = Depends(get_container),
        limit: int = Query(default=25, ge=1, le=100),
        offset: str | None = Query(default=None, description="opaque next-page token"),
    ) -> dict[str, object]:
        """Page through the records stored in one collection (read-only browse)."""
        records, next_offset = c.vector_store.scroll(name.value, limit=limit, offset=offset)
        return {
            "records": [_record_view(r, name.value) for r in records],
            "next_offset": next_offset,
        }

    @app.post("/corpus/ingest", tags=["corpus"])
    def corpus_ingest(
        req: CorpusIngestRequest, c: Container = Depends(get_container)
    ) -> dict[str, object]:
        """Add or update grounding records. Returns what was ingested plus the
        refreshed per-collection counts so the UI updates without a second call."""
        try:
            report = c.ingestor.ingest_records(
                req.collection, req.records, source_label="ui"
            )
        except ValueError as exc:  # a record missing 'text', bad shape, …
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {
            "documents": report.documents,
            "chunks": report.chunks,
            "sources": sorted(report.sources),
            "counts": {col.value: c.vector_store.count(col.value) for col in Collection},
        }

    @app.get("/corpus/chunkers", tags=["corpus"])
    def corpus_chunkers(c: Container = Depends(get_container)) -> dict[str, object]:
        """The chunkers available for document ingest and the per-format defaults —
        so the UI can offer the right strategy and show which one a format uses."""
        return {
            "chunkers": sorted(CHUNKER_REGISTRY),
            "default": c.settings.chunking.default,
            "by_format": c.settings.chunking.by_format,
            "params": c.settings.chunking.params,
        }

    @app.post("/corpus/preview", tags=["corpus"])
    def corpus_preview(
        req: DocumentIngestRequest, c: Container = Depends(get_container)
    ) -> dict[str, object]:
        """Dry-run: split a document with the chosen chunker + knobs and return the
        resulting chunks (with their header breadcrumbs) WITHOUT embedding or
        storing anything. Lets a curator tune the chunker and see the split before
        committing. Cheap — no model, no vector store."""
        try:
            chunker = _resolve_document_chunker(c.settings, req)
            doc = _document_from_request(req)
            chunks = chunker.chunk(
                doc.text, collection=req.collection, source=doc.source, metadata=doc.metadata
            )
        except ValueError as exc:  # unknown chunker, bad params
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        limit = 60
        return {
            "total": len(chunks),
            "truncated": len(chunks) > limit,
            "chunks": [_chunk_view(ch) for ch in chunks[:limit]],
        }

    @app.post("/corpus/ingest/document", tags=["corpus"])
    def corpus_ingest_document(
        req: DocumentIngestRequest, c: Container = Depends(get_container)
    ) -> dict[str, object]:
        """Ingest one whole document (a book chapter, an exemplar lesson) with a
        structure-aware chunker — the Markdown path preserves the header hierarchy
        as ``heading_path`` on every chunk. Idempotent, like every ingest path."""
        try:
            chunker = _resolve_document_chunker(c.settings, req)
            doc = _document_from_request(req)
            report = c.ingestor.ingest_documents(req.collection, [doc], chunker=chunker)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {
            "documents": report.documents,
            "chunks": report.chunks,
            "sources": sorted(report.sources),
            "counts": {col.value: c.vector_store.count(col.value) for col in Collection},
        }

    @app.post("/corpus/collections/{name}/delete", tags=["corpus"])
    def corpus_delete(
        name: Collection,
        req: CorpusDeleteRequest,
        c: Container = Depends(get_container),
    ) -> dict[str, object]:
        """Delete records by id (from the browse view). Destructive — the UI
        confirms first."""
        deleted = c.vector_store.delete(name.value, req.ids)
        return {"deleted": deleted, "count": c.vector_store.count(name.value)}

    # ── QA chatbot: streaming, persistent, advanced-RAG over the collections ──
    @app.get("/chat", response_class=HTMLResponse, include_in_schema=False)
    def chat_page() -> str:
        """The chatbot web app — a single self-contained page (streaming answers,
        citation previews, scoped/broad modes)."""
        if _UI_CHAT.exists():
            return _UI_CHAT.read_text(encoding="utf-8")
        return "<h1>LessonForge chat</h1><p>UI asset missing. See /docs for the API.</p>"

    def _get_conversation(c: Container, conversation_id: str) -> Conversation:
        conv = c.get_chat_store().get(conversation_id)
        if conv is None:
            raise HTTPException(status_code=404, detail="conversation not found")
        return conv

    @app.get("/chat/config", tags=["chat"])
    def chat_config(c: Container = Depends(get_container)) -> dict[str, object]:
        """What the chat UI needs to render its controls: the searchable
        collections, which tags become scoped-mode filters, and the default mode."""
        r = c.settings.chat.retrieval
        return {
            "collections": r.collections,
            "filter_fields": r.filter_fields,
            "default_mode": r.default_mode,
        }

    @app.get("/chat/conversations", response_model=list[Conversation], tags=["chat"])
    def list_conversations(
        c: Container = Depends(get_container), owner_id: str = Query(default="default")
    ) -> list[Conversation]:
        """A teacher's conversations, most-recently-updated first."""
        return c.get_chat_store().list(owner_id)

    @app.post("/chat/conversations", response_model=Conversation, tags=["chat"])
    def create_conversation(
        req: CreateConversationRequest, c: Container = Depends(get_container)
    ) -> Conversation:
        """Start a conversation with its default retrieval scope (grade/subject/
        class/collections + scoped|broad mode)."""
        conv = Conversation(
            owner_id=req.owner_id,
            title=req.title or "New chat",
            defaults=req.defaults,
        )
        return c.get_chat_store().create(conv)

    @app.get("/chat/conversations/{conversation_id}", tags=["chat"])
    def get_conversation(
        conversation_id: str, c: Container = Depends(get_container)
    ) -> dict[str, object]:
        """A conversation with its full message history (for reloading a thread)."""
        conv = _get_conversation(c, conversation_id)
        msgs = c.get_chat_store().messages(conversation_id)
        return {
            "conversation": conv.model_dump(by_alias=True),
            "messages": [m.model_dump() for m in msgs],
        }

    @app.patch("/chat/conversations/{conversation_id}", response_model=Conversation, tags=["chat"])
    def update_conversation(
        conversation_id: str,
        req: UpdateConversationRequest,
        c: Container = Depends(get_container),
    ) -> Conversation:
        """Rename a conversation and/or change its default retrieval scope."""
        _get_conversation(c, conversation_id)
        updated = c.get_chat_store().update(
            conversation_id, title=req.title, defaults=req.defaults
        )
        if updated is None:  # pragma: no cover - race: deleted between get and update
            raise HTTPException(status_code=404, detail="conversation not found")
        return updated

    @app.delete("/chat/conversations/{conversation_id}", tags=["chat"])
    def delete_conversation(
        conversation_id: str, c: Container = Depends(get_container)
    ) -> dict[str, bool]:
        """Delete a conversation and all its messages."""
        return {"deleted": c.get_chat_store().delete(conversation_id)}

    @app.post("/chat/conversations/{conversation_id}/message", tags=["chat"])
    def send_message(
        conversation_id: str,
        req: ChatMessageRequest,
        c: Container = Depends(get_container),
    ) -> StreamingResponse:
        """Ask a question and stream the grounded answer back as Server-Sent
        Events: ``token`` deltas, then a ``sources`` event carrying the citations
        (each with its chunk text for preview), then ``done`` (or ``error``). The
        optional ``mode``/``filters`` in the body override the conversation's
        default scope for this one message (the two-modes control)."""
        conv = _get_conversation(c, conversation_id)

        def event_stream():
            try:
                for event in c.get_chat_pipeline().stream(conv, req):
                    yield _sse(event)
            except Exception as exc:  # pragma: no cover - defensive top-level guard
                yield _sse(ChatEvent("error", {"message": str(exc)}))

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app


def _sse(event: ChatEvent) -> str:
    """Encode one pipeline event as an SSE frame."""
    return f"event: {event.type}\ndata: {json.dumps(event.data)}\n\n"


app = create_app()
