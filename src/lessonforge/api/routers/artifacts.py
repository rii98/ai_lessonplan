"""Targeted artifacts (quiz / worksheet / slides) without a whole lesson.
Requires ``author``."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, Query
from fastapi.responses import Response
from pydantic import ValidationError

from ...activity import events as act
from ...auth.principal import Perm
from ...container import Container
from ...domain.artifacts import Quiz, Slides, Worksheet
from ...domain.assessment import MAX_PER_TYPE, MAX_TOTAL, PRESETS, QUESTION_TYPES, AssessmentSpec
from ...domain.ldd import IntakeRequest, NormalizedBrief
from ...export import ArtifactKind, formats_for
from ...request_context import RequestContext
from ...services.artifact_generation import generatable_kinds
from ...services.assessment_retrieval import plan_passes
from ...services.assessment_scope import scope_from_lesson
from ..activity import emit
from ..deps import get_container, request_ctx, require, teacher_profile
from ..errors import unprocessable
from ..helpers import file_response
from ..schemas import ArtifactGenerateRequest, LessonAssessmentRequest, format_errors

router = APIRouter(prefix="/api/v1/artifacts", tags=["artifacts"],
                   dependencies=[Depends(require(Perm.AUTHOR))])

# Maps a targeted artifact kind to the IR model its generate/export endpoints use.
_ARTIFACT_MODELS: dict[ArtifactKind, type] = {
    ArtifactKind.quiz: Quiz,
    ArtifactKind.worksheet: Worksheet,
    ArtifactKind.slides: Slides,
}


def _generatable(kind: ArtifactKind) -> None:
    if kind not in _ARTIFACT_MODELS or kind not in generatable_kinds():
        raise unprocessable(f"{kind.value!r} is not generatable on its own. "
                            f"Generatable: {[k.value for k in generatable_kinds()]}.")


def _quiz_event(c: Container, kind: ArtifactKind, artifact: Any, source: str) -> None:
    if kind not in (ArtifactKind.quiz, ArtifactKind.worksheet):
        return
    questions = getattr(artifact, "questions", None) or []
    spec = getattr(artifact, "spec", None)
    ref = getattr(artifact, "curriculum_ref", None)
    emit(c, "quiz.created", act.QuizCreated(
        source=source, questions=len(questions),
        question_types=list(dict.fromkeys(str(getattr(q, "type", "")) for q in questions
                                          if getattr(q, "type", None)))[:20],
        difficulty=str(spec.difficulty) if spec is not None and getattr(spec, "difficulty", None)
        else None,
        subject=getattr(ref, "subject", None)))


def generate_assessment(c: Container, ctx: RequestContext, kind: ArtifactKind,
                        brief: NormalizedBrief, spec: AssessmentSpec, scope: Any, *,
                        source: str) -> Any:
    """Shared tail of the from-lesson / from-unit routes."""
    _generatable(kind)
    generator = c.artifact_generator(kind)
    if not generator.supports_spec:
        raise unprocessable(f"a {kind.value} has no question blueprint.")
    brief = teacher_profile(c, ctx).personalize(brief)
    try:
        artifact = generator.generate(brief, spec=spec, scope=scope)
    except ValueError as exc:
        raise unprocessable(str(exc)) from exc
    _quiz_event(c, kind, artifact, source)
    return artifact


@router.get("/manifest")
def artifacts_manifest() -> dict[str, object]:
    """Which artifacts can be generated standalone, and in which export formats."""
    return {"generatable": [{"kind": k.value, "formats": formats_for(k)}
                            for k in generatable_kinds()]}


@router.get("/question-types")
def question_types() -> dict[str, object]:
    """The question-type catalogue the quiz/worksheet builder renders from."""
    return {
        "types": [
            {"type": t.value, "label": m.label, "hint": m.hint, "default_marks": m.default_marks,
             "default_count": m.default_count, "retrieval": m.retrieval}
            for t, m in QUESTION_TYPES.items()
        ],
        "difficulties": ["easy", "medium", "hard", "mixed"],
        "scopes": [
            {"value": "auto", "label": "Auto (recommended)",
             "help": "Facts from focused passages; explanations from whole sections."},
            {"value": "focused", "label": "Focused",
             "help": "Short passages only — best for recall-style quizzes."},
            {"value": "section", "label": "Whole sections",
             "help": "Full textbook sections — best when questions need context."},
            {"value": "chapter", "label": "Whole chapter",
             "help": "Revision test: questions may range across the chapter."},
        ],
        "presets": PRESETS,
        "limits": {"max_per_type": MAX_PER_TYPE, "max_total": MAX_TOTAL},
    }


@router.post("/retrieval-plan")
def retrieval_plan(spec: AssessmentSpec) -> dict[str, object]:
    """How a blueprint will be grounded — free; no LLM."""
    return {"passes": [p.describe() for p in plan_passes(spec)],
            "total_questions": spec.total_questions, "total_marks": spec.total_marks}


@router.post("/{kind}/generate")
def generate_artifact(kind: ArtifactKind, req: ArtifactGenerateRequest,
                      c: Container = Depends(get_container),
                      ctx: RequestContext = Depends(request_ctx)):
    """Generate a single artifact straight from a brief; returns the editable IR."""
    _generatable(kind)
    profile = teacher_profile(c, ctx)
    try:
        request = profile.apply_defaults(IntakeRequest(**req.model_dump(exclude={"spec"})))
        brief = profile.personalize(c.intake.normalize(request))
    except ValueError as exc:  # intake couldn't determine topic/grade
        raise unprocessable(str(exc)) from exc
    generator = c.artifact_generator(kind)
    if req.spec is not None and not generator.supports_spec:
        raise unprocessable(f"a {kind.value} has no question blueprint; omit `spec`.")
    try:
        artifact = generator.generate(brief, spec=req.spec)
    except ValueError as exc:  # could not assemble a valid artifact
        raise unprocessable(str(exc)) from exc
    _quiz_event(c, kind, artifact, "brief")
    return artifact


@router.post("/{kind}/from-lesson")
def assessment_from_lesson(kind: ArtifactKind, req: LessonAssessmentRequest,
                           c: Container = Depends(get_container),
                           ctx: RequestContext = Depends(request_ctx)):
    """A quiz/worksheet for ONE lesson, reusing its objectives."""
    brief, scope = scope_from_lesson(req.ldd)
    return generate_assessment(c, ctx, kind, brief, req.spec, scope, source="lesson")


@router.post("/{kind}/export")
def export_generated_artifact(kind: ArtifactKind, payload: dict[str, Any] = Body(...),
                              c: Container = Depends(get_container),
                              fmt: str | None = Query(default=None, max_length=16)) -> Response:
    """Render a standalone artifact IR (possibly hand-edited) into a file."""
    _generatable(kind)
    try:
        ir = _ARTIFACT_MODELS[kind].model_validate(payload)
    except ValidationError as exc:
        raise unprocessable("invalid artifact", format_errors(exc)) from exc
    try:
        art = c.exporter.render_artifact(kind, ir, fmt=fmt)
    except ValueError as exc:
        raise unprocessable(str(exc)) from exc
    emit(c, "artifact.exported", act.ArtifactExported(
        artifact=kind.value, format=fmt or art.filename.rsplit(".", 1)[-1]))
    return file_response(art)


@router.get("/{kind}/refine/targets")
def artifact_refine_targets(kind: ArtifactKind) -> dict[str, object]:
    """The reprompt-able parts of an artifact and their edit blast radius."""
    _generatable(kind)
    from ...domain.artifact_sections import ARTIFACT_SECTIONS

    return {
        "sections": [{"target": s.name, "coupled": list(s.coupled), "leaf": s.is_leaf,
                      "grounded": s.grounded} for s in ARTIFACT_SECTIONS[kind.value].values()],
        "whole_document": "*",
    }


@router.post("/{kind}/refine")
def refine_artifact(kind: ArtifactKind, payload: dict[str, Any] = Body(...),
                    c: Container = Depends(get_container)):
    """Reprompt one part of an artifact (or "*") and PROPOSE an improved IR."""
    _generatable(kind)
    target = str(payload.get("target", "")).strip()
    instruction = str(payload.get("instruction", "")).strip()
    if not target or not instruction:
        raise unprocessable("`target` and `instruction` are required")
    try:
        ir = _ARTIFACT_MODELS[kind].model_validate(payload.get("artifact", {}))
    except ValidationError as exc:
        raise unprocessable("invalid artifact", format_errors(exc)) from exc
    try:
        result = c.artifact_refiner(kind).refine(ir, target, instruction)
    except ValueError as exc:  # unknown target
        raise unprocessable(str(exc)) from exc
    return {"ok": result.ok, "target": result.target, "instruction": result.instruction,
            "candidate": result.candidate.model_dump() if result.candidate else None,
            "diff": result.diff, "cascaded": result.cascaded, "errors": result.errors,
            "notes": result.notes}
