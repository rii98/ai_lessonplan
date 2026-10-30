"""Lessons: generate, preview intake, critique, refine, validate, export; the
teacher profile; grounding preview. Requires ``author``."""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Body, Depends, Query
from fastapi.responses import Response
from pydantic import ValidationError

from ...activity import events as act
from ...auth.principal import Perm
from ...container import Container
from ...domain.ldd import IntakeRequest, LessonDesignDocument, NormalizedBrief
from ...domain.profile import TeacherProfile
from ...domain.refine import RefineRequest, RefineResult
from ...domain.rubric import Critique
from ...domain.sections import LDD_SECTIONS, WHOLE_DOCUMENT
from ...export import ArtifactKind
from ...request_context import RequestContext
from ..activity import cost, emit
from ..deps import get_container, request_ctx, require, teacher_profile
from ..errors import unprocessable
from ..helpers import file_response, grounding_tree
from ..schemas import (
    GenerateRequest,
    GroundingPreviewRequest,
    RefineLessonRequest,
    ValidationResult,
    format_errors,
)

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require(Perm.AUTHOR))])


@router.post("/lessons/generate", response_model=LessonDesignDocument, tags=["lessons"])
def generate_lesson(req: GenerateRequest, c: Container = Depends(get_container),
                    ctx: RequestContext = Depends(request_ctx)) -> LessonDesignDocument:
    """Full pipeline: intake → enrichment → critique & revise → validated LDD.
    The teacher's stored profile (and Edboox assignments) fill unset fields."""
    started = time.perf_counter()
    try:
        ldd = c.pipeline.run(IntakeRequest(**req.model_dump()), profile=teacher_profile(c, ctx))
    except ValueError as exc:  # intake couldn't determine topic/grade
        raise unprocessable(str(exc)) from exc
    emit(c, "lesson.generated", act.Generated(grade=ldd.curriculum_ref.grade,
                                              subject=ldd.curriculum_ref.subject, **cost(started)))
    return ldd


@router.post("/lessons/intake", response_model=NormalizedBrief, tags=["lessons"])
def intake_lesson(req: GenerateRequest, c: Container = Depends(get_container),
                  ctx: RequestContext = Depends(request_ctx)) -> NormalizedBrief:
    """US-3 preview: normalize a raw request / pasted plan into a brief, without
    generating. Profile defaults apply, so the preview matches what generate sees."""
    profile = teacher_profile(c, ctx)
    try:
        request = profile.apply_defaults(IntakeRequest(**req.model_dump(exclude={"spec"})))
        return profile.personalize(c.intake.normalize(request))
    except ValueError as exc:
        raise unprocessable(str(exc)) from exc


@router.post("/lessons/critique", response_model=Critique, tags=["lessons"])
def critique_lesson(ldd: LessonDesignDocument, c: Container = Depends(get_container)) -> Critique:
    """Score an already-generated LDD against the anti-generic rubric."""
    return c.pipeline.critique(ldd)


@router.get("/lessons/refine/targets", tags=["lessons"])
def refine_targets() -> dict[str, object]:
    """The reprompt-able sections and their edit blast radius."""
    return {
        "sections": [{"target": s.name, "coupled": list(s.coupled), "leaf": s.is_leaf}
                     for s in LDD_SECTIONS.values()],
        "whole_document": WHOLE_DOCUMENT,
    }


@router.post("/lessons/refine", response_model=RefineResult, tags=["lessons"])
def refine_lesson(req: RefineLessonRequest, c: Container = Depends(get_container)) -> RefineResult:
    """Reprompt one section (or the whole lesson) and PROPOSE an improved,
    re-validated LDD; a change that would break a guardrail is cascade-repaired or
    rejected with the reason, never silently forced through."""
    try:
        return c.refiner.refine(req.ldd, RefineRequest(target=req.target,
                                                       instruction=req.instruction))
    except ValueError as exc:  # unknown target
        raise unprocessable(str(exc)) from exc


@router.post("/lessons/validate", response_model=ValidationResult, tags=["lessons"])
def validate_ldd(payload: dict[str, Any] = Body(...)) -> ValidationResult:
    """Validate an edited LDD against the guardrails WITHOUT exporting. Returns 200
    even when invalid so the editor shows field errors inline."""
    try:
        LessonDesignDocument.model_validate(payload)
    except ValidationError as exc:
        return ValidationResult(valid=False, errors=format_errors(exc))
    return ValidationResult(valid=True)


@router.post("/grounding/preview", tags=["grounding"])
def grounding_preview(req: GroundingPreviewRequest,
                      c: Container = Depends(get_container)) -> dict[str, Any]:
    """WHICH chunks — and their heading hierarchy — a lesson on this topic grounds
    in, via the same deterministic retrieval generation uses (within the caller's
    knowledge scope). No LLM."""
    empty = {"grounded": False, "has_authoritative": False, "sources": [], "collections": []}
    grounding = getattr(c, "grounding", None)
    if grounding is None:
        return empty
    query = f"{req.topic} grade {req.grade} {req.subject}" if req.grade and req.subject else req.topic
    try:
        bundle = grounding.ground(query=query, grade=req.grade, subject=req.subject,
                                  framework=req.framework)
    except Exception:
        return empty
    return grounding_tree(bundle)


# ── export: LDD → downloadable artifacts ─────────────────────────────────────
@router.get("/export/manifest", tags=["export"])
def export_manifest(c: Container = Depends(get_container)) -> dict[str, object]:
    """What the export subsystem can produce and in which formats."""
    return c.exporter.manifest()


@router.post("/lessons/export/bundle/zip", tags=["export"])
def export_bundle(ldd: LessonDesignDocument, c: Container = Depends(get_container)) -> Response:
    """One-click zip of every configured artifact."""
    emit(c, "artifact.exported", act.ArtifactExported(artifact="bundle", format="zip"))
    return file_response(c.exporter.bundle(ldd))


@router.post("/lessons/export/{kind}", tags=["export"])
def export_artifact(kind: ArtifactKind, ldd: LessonDesignDocument,
                    c: Container = Depends(get_container),
                    fmt: str | None = Query(default=None, max_length=16,
                                            description="Override the configured format"),
                    ) -> Response:
    """Render one artifact from an already-generated LDD."""
    try:
        art = c.exporter.render(kind, ldd, fmt=fmt)
    except ValueError as exc:  # unknown/unsupported format
        raise unprocessable(str(exc)) from exc
    emit(c, "artifact.exported", act.ArtifactExported(artifact=kind.value,
                                                      format=fmt or art.filename.rsplit(".", 1)[-1]))
    return file_response(art)


# ── teacher profile ──────────────────────────────────────────────────────────
@router.get("/profile", response_model=TeacherProfile, tags=["profile"])
def get_profile(c: Container = Depends(get_container),
                ctx: RequestContext = Depends(request_ctx)) -> TeacherProfile:
    """The caller's stored preferences (unset defaults filled from Edboox context)."""
    return teacher_profile(c, ctx)


@router.put("/profile", response_model=TeacherProfile, tags=["profile"])
def put_profile(profile: TeacherProfile, c: Container = Depends(get_container),
                ctx: RequestContext = Depends(request_ctx)) -> TeacherProfile:
    """Save the caller's preferences (the owner is the caller, whatever the body says)."""
    assert ctx.principal is not None
    return c.profile_store.save(ctx.principal.id, profile)
