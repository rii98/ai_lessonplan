"""Documents (saved lessons) and units, with version history and school sharing.
Requires ``author``.

Read routes (view, history, export, copy) allow the owner or — for a plan shared
with the school — a colleague whose signed school matches; every other route is
owner-only. Invisible → 404; visible but not yours → 403 (§17).
"""

from __future__ import annotations

import time
from typing import Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from pydantic import ValidationError

from ...activity import events as act
from ...auth.principal import Perm, Principal
from ...container import Container
from ...domain.document import DocumentKind, DocumentVersionInfo, StoredDocument, Visibility
from ...domain.ldd import IntakeRequest, LessonDesignDocument
from ...domain.refine import RefineRequest
from ...domain.unit import UnitDesignDocument, UnitRequest
from ...export import ArtifactKind
from ...pagination import DEFAULT_LIMIT, MAX_LIMIT, Keyset, PageOut
from ...request_context import RequestContext
from ...services.assessment_scope import scope_from_unit
from ...services.unit_coherence import UnitCoherence
from ..activity import cost, emit
from ..deps import current_principal, get_container, request_ctx, require, teacher_profile
from ..errors import unprocessable
from ..helpers import file_response
from ..schemas import (
    CopiedDocument,
    CopyRequest,
    DocumentRefineRequest,
    DocumentRefineResponse,
    DocumentView,
    GenerateUnitRequest,
    PlanResponse,
    SavedLesson,
    SavedUnit,
    SaveLessonRequest,
    UnitAssessmentRequest,
    UnitView,
    VisibilityRequest,
    format_errors,
)
from .artifacts import generate_assessment

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require(Perm.AUTHOR))])


def _visibility_event(c: Container, doc: StoredDocument, type_: str) -> None:
    emit(c, type_, act.DocumentVisibility(kind=doc.kind.value, grade=doc.grade,
                                          subject=doc.subject),
         resource_type="document", resource_id=doc.id)


# ── documents ────────────────────────────────────────────────────────────────
@router.post("/documents/lessons", response_model=SavedLesson, tags=["documents"])
def create_lesson_document(req: SaveLessonRequest, c: Container = Depends(get_container),
                           ctx: RequestContext = Depends(request_ctx)) -> SavedLesson:
    """Generate a lesson through the full pipeline and persist it as a new document
    with its first (``generate``) version."""
    started = time.perf_counter()
    gen = req.model_dump(exclude={"title"})
    try:
        ldd = c.pipeline.run(IntakeRequest(**gen), profile=teacher_profile(c, ctx))
    except ValueError as exc:
        raise unprocessable(str(exc)) from exc
    assert ctx.principal is not None
    doc, version = c.get_document_service().save_lesson(ldd, owner=ctx.principal, title=req.title)
    emit(c, "lesson.generated", act.Generated(grade=doc.grade, subject=doc.subject,
                                              **cost(started)),
         resource_type="document", resource_id=doc.id)
    return SavedLesson(document=doc, version_id=version.id, ldd=ldd)


@router.get("/documents", response_model=PageOut[StoredDocument], tags=["documents"])
def list_documents(
    scope: Literal["mine", "school"] = Query("mine"),
    kind: DocumentKind | None = None,
    grade: int | None = Query(None, ge=1, le=12),
    subject: str | None = Query(None, max_length=120),
    q: str | None = Query(None, max_length=120, description="title contains (school scope)"),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    cursor: str | None = Query(None, max_length=512),
    c: Container = Depends(get_container), p: Principal = Depends(current_principal),
) -> PageOut[StoredDocument]:
    """``mine``: the caller's documents, most recently updated first. ``school``:
    plans colleagues shared with the caller's school, newest share first."""
    svc, key = c.get_document_service(), Keyset.decode(cursor)
    page = (svc.list_mine(p, kind=kind, limit=limit, cursor=key) if scope == "mine" else
            svc.list_school(p, kind=kind, grade=grade, subject=subject, q=q, limit=limit,
                            cursor=key))
    return PageOut(items=page.items, next_cursor=page.next_cursor)


@router.get("/documents/{document_id}", response_model=DocumentView, tags=["documents"])
def get_document(document_id: str,
                 version: str | None = Query(None, max_length=64,
                                             description="a version id; default = head"),
                 c: Container = Depends(get_container),
                 p: Principal = Depends(current_principal)) -> DocumentView:
    svc = c.get_document_service()
    doc, ldd = (svc.lesson_version(document_id, version, p) if version
                else svc.head_lesson(document_id, p))
    return DocumentView(document=doc, ldd=ldd)


@router.get("/documents/{document_id}/versions", response_model=PageOut[DocumentVersionInfo],
            tags=["documents"])
def document_versions(document_id: str, c: Container = Depends(get_container),
                      p: Principal = Depends(current_principal)) -> PageOut[DocumentVersionInfo]:
    """The full edit history, oldest first, without snapshots (fetch one version
    with ``GET /documents/{id}?version=``)."""
    return PageOut(items=c.get_document_service().versions(document_id, p))


@router.patch("/documents/{document_id}", response_model=StoredDocument, tags=["documents"])
def set_document_visibility(document_id: str, req: VisibilityRequest,
                            c: Container = Depends(get_container),
                            p: Principal = Depends(current_principal)) -> StoredDocument:
    """Share with the caller's school (colleagues may read and copy, never edit) or
    make private again (removed from colleagues' lists immediately)."""
    doc = c.get_document_service().set_visibility(document_id, req.visibility, p)
    _visibility_event(c, doc, "document.shared" if req.visibility == Visibility.school
                      else "document.unshared")
    return doc


@router.post("/documents/{document_id}/copy", response_model=CopiedDocument, tags=["documents"])
def copy_document(document_id: str, req: CopyRequest | None = None,
                  c: Container = Depends(get_container),
                  p: Principal = Depends(current_principal)) -> CopiedDocument:
    """Copy a plan you can read into a new private document of your own."""
    svc = c.get_document_service()
    source = svc.readable(document_id, p)
    doc, version = svc.copy(document_id, p, title=req.title if req else None)
    emit(c, "document.copied", act.DocumentCopied(
        kind=doc.kind.value, grade=doc.grade, subject=doc.subject,
        source_owner=None if source.owner_id == p.id else _subject_of(c, source.owner_id)),
        resource_type="document", resource_id=doc.id)
    return CopiedDocument(document=doc, version_id=version.id)


def _subject_of(c: Container, principal_id: str) -> str | None:
    rec = c.platform.directory.get(principal_id)
    return rec.subject if rec else None


@router.post("/documents/{document_id}/refine", response_model=DocumentRefineResponse,
             tags=["documents"])
def refine_document(document_id: str, req: DocumentRefineRequest,
                    c: Container = Depends(get_container),
                    p: Principal = Depends(current_principal)) -> DocumentRefineResponse:
    """Refine the head lesson (owner only). Proposes by default; with
    ``commit=true`` an accepted refine is appended as a new version."""
    svc = c.get_document_service()
    _, head = svc.refinable_lesson(document_id, p)
    try:
        result = c.refiner.refine(head, RefineRequest(target=req.target,
                                                      instruction=req.instruction))
    except ValueError as exc:
        raise unprocessable(str(exc)) from exc
    version_id: str | None = None
    if req.commit and result.ok:
        version = svc.commit_refine(document_id, result, p)
        version_id = version.id if version else None
        emit(c, "document.refined", act.DocumentRefined(section=_section(req.target), kind="ai"),
             resource_type="document", resource_id=document_id)
    return DocumentRefineResponse(result=result, version_id=version_id)


def _section(target: str) -> str:
    return "whole" if target == "*" else target[:64]


@router.post("/documents/{document_id}/manual", response_model=SavedLesson, tags=["documents"])
def commit_manual_edit(document_id: str, ldd: LessonDesignDocument,
                       c: Container = Depends(get_container),
                       p: Principal = Depends(current_principal)) -> SavedLesson:
    """Persist a teacher's direct edit as a new ``manual`` version (owner only)."""
    svc = c.get_document_service()
    version = svc.commit_manual(document_id, ldd, p)
    emit(c, "document.refined", act.DocumentRefined(section="whole", kind="manual"),
         resource_type="document", resource_id=document_id)
    return SavedLesson(document=svc.readable(document_id, p), version_id=version.id, ldd=ldd)


@router.post("/documents/{document_id}/undo", response_model=StoredDocument, tags=["documents"])
def undo_document(document_id: str, c: Container = Depends(get_container),
                  p: Principal = Depends(current_principal)) -> StoredDocument:
    """Move the head back to the previous version. 409 at the root."""
    return c.get_document_service().undo(document_id, p)


@router.post("/documents/{document_id}/redo", response_model=StoredDocument, tags=["documents"])
def redo_document(document_id: str, c: Container = Depends(get_container),
                  p: Principal = Depends(current_principal)) -> StoredDocument:
    """Move the head forward again after an undo. 409 when there is no child."""
    return c.get_document_service().redo(document_id, p)


@router.delete("/documents/{document_id}", tags=["documents"])
def delete_document(document_id: str, c: Container = Depends(get_container),
                    p: Principal = Depends(current_principal)) -> dict[str, bool]:
    """Delete a document and its entire history (owner only). Colleagues' copies live on."""
    c.get_document_service().delete(document_id, p)
    return {"deleted": True}


# ── units: plan-and-expand multi-day units ───────────────────────────────────
def _unit_request_from(unit: UnitDesignDocument) -> UnitRequest:
    d0 = unit.days[0]
    return UnitRequest(topic=unit.title, grade=unit.curriculum_ref.grade,
                       subject=unit.curriculum_ref.subject, num_days=len(unit.days),
                       duration_min=d0.duration_min, language=d0.language, framework=d0.framework)


@router.post("/units/plan", response_model=PlanResponse, tags=["units"])
def plan_unit(req: UnitRequest, c: Container = Depends(get_container),
              ctx: RequestContext = Depends(request_ctx)) -> PlanResponse:
    """The unit spine (the arc) + syllabus-coverage report, for review BEFORE the
    expensive per-day expansion."""
    try:
        plan, coverage = c.unit_planner.plan_with_coverage(req, profile=teacher_profile(c, ctx))
    except ValueError as exc:
        raise unprocessable(str(exc)) from exc
    return PlanResponse(plan=plan, coverage=coverage)


@router.post("/units/generate", response_model=SavedUnit, tags=["units"])
def generate_unit(req: GenerateUnitRequest, c: Container = Depends(get_container),
                  ctx: RequestContext = Depends(request_ctx)) -> SavedUnit:
    """Plan and expand a full multi-day unit, then persist it with version history."""
    started = time.perf_counter()
    profile = teacher_profile(c, ctx)
    unit_req = UnitRequest(**req.model_dump(exclude={"title", "plan"}))
    try:
        unit = (c.unit_generator.expand(req.plan, unit_req, profile=profile)
                if req.plan is not None else c.unit_generator.generate(unit_req, profile=profile))
    except ValueError as exc:
        raise unprocessable(str(exc)) from exc
    assert ctx.principal is not None
    doc, version = c.get_document_service().save_unit(unit, owner=ctx.principal, title=req.title)
    emit(c, "unit.generated", act.Generated(grade=doc.grade, subject=doc.subject,
                                            days=len(unit.days), **cost(started)),
         resource_type="document", resource_id=doc.id)
    return SavedUnit(document=doc, version_id=version.id, unit=unit)


@router.get("/units/{document_id}", response_model=UnitView, tags=["units"])
def get_unit(document_id: str,
             version: str | None = Query(None, max_length=64,
                                         description="a version id; default = head"),
             c: Container = Depends(get_container),
             p: Principal = Depends(current_principal)) -> UnitView:
    svc = c.get_document_service()
    doc, unit = svc.unit_version(document_id, version, p) if version else svc.head_unit(document_id, p)
    return UnitView(document=doc, unit=unit)


@router.post("/units/{document_id}/days/{day}/regenerate", response_model=SavedUnit,
             tags=["units"])
def regenerate_unit_day(document_id: str, day: int, c: Container = Depends(get_container),
                        ctx: RequestContext = Depends(request_ctx)) -> SavedUnit:
    """Rebuild ONE day against the same arc and commit it as a new version."""
    assert ctx.principal is not None
    svc = c.get_document_service()
    _, unit = svc.head_unit(document_id, ctx.principal, write=True)
    try:
        updated = c.unit_generator.regenerate_day(unit, day, _unit_request_from(unit),
                                                  profile=teacher_profile(c, ctx))
    except ValueError as exc:
        raise unprocessable(str(exc)) from exc
    version = svc.commit_unit(document_id, updated, ctx.principal,
                              instruction=f"regenerated day {day}")
    return SavedUnit(document=svc.readable(document_id, ctx.principal), version_id=version.id,
                     unit=updated)


@router.put("/units/{document_id}/days/{day}", response_model=SavedUnit, tags=["units"])
def edit_unit_day(document_id: str, day: int, ldd: LessonDesignDocument,
                  c: Container = Depends(get_container),
                  p: Principal = Depends(current_principal)) -> SavedUnit:
    """Replace ONE day's lesson with a teacher-edited LDD, re-run coherence and
    commit a new manual version (owner only)."""
    svc = c.get_document_service()
    _, unit = svc.head_unit(document_id, p, write=True)
    if not 1 <= day <= len(unit.days):
        raise unprocessable(f"day {day} is out of range 1..{len(unit.days)}")
    new_days = list(unit.days)
    new_days[day - 1] = ldd
    try:
        candidate = UnitDesignDocument.model_validate(
            unit.model_copy(update={"days": new_days}).model_dump(mode="json"))
    except ValidationError as exc:
        raise unprocessable("the edited day breaks the unit", format_errors(exc)) from exc
    updated = UnitCoherence().apply(candidate)
    version = svc.commit_unit(document_id, updated, p, instruction=f"edited day {day}")
    return SavedUnit(document=svc.readable(document_id, p), version_id=version.id, unit=updated)


@router.post("/units/{document_id}/export", tags=["units"])
def export_unit(document_id: str, c: Container = Depends(get_container),
                p: Principal = Depends(current_principal)) -> Response:
    """Download the whole unit as a zip of per-day lesson plans (a read)."""
    _, unit = c.get_document_service().head_unit(document_id, p)
    emit(c, "artifact.exported", act.ArtifactExported(artifact="unit", format="zip"),
         resource_type="document", resource_id=document_id)
    return file_response(c.exporter.unit_zip(unit))


@router.post("/units/{document_id}/assessment/{kind}", tags=["units"])
def assessment_from_unit(document_id: str, kind: ArtifactKind, req: UnitAssessmentRequest,
                         c: Container = Depends(get_container),
                         ctx: RequestContext = Depends(request_ctx)):
    """An end-of-unit (or chosen-days) test from a saved unit."""
    assert ctx.principal is not None
    _, unit = c.get_document_service().head_unit(document_id, ctx.principal)
    try:
        brief, scope = scope_from_unit(unit, req.days)
    except ValueError as exc:
        raise unprocessable(str(exc)) from exc
    return generate_assessment(c, ctx, kind, brief, req.spec, scope, source="unit")
