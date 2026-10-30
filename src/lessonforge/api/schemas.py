"""Request/response models of the public ``/api/v1`` contract.

Ownership is never a request field: every document, conversation and profile is
owned by the authenticated caller (D7).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, model_validator

from ..domain.assessment import AssessmentSpec
from ..domain.document import StoredDocument, Visibility
from ..domain.ldd import LessonDesignDocument
from ..domain.refine import RefineResult
from ..domain.unit import CoverageReport, UnitDesignDocument, UnitPlan, UnitRequest
from ..knowledge.scope import GLOBAL_SPACE
from ..rag.documents import Collection


class ValidationResult(BaseModel):
    """Result of validating an edited LDD before export. Returned with HTTP 200
    even when invalid, so the editor UI can render field-level errors inline
    rather than treating a normal editing state as a request failure."""

    valid: bool
    errors: list[dict[str, str]] = Field(default_factory=list)


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


class ArtifactGenerateRequest(GenerateRequest):
    """A targeted-artifact request: the usual brief plus, for a quiz/worksheet, the
    author's question blueprint (types × counts × difficulty × instructions)."""

    spec: AssessmentSpec | None = None


class LessonAssessmentRequest(BaseModel):
    """Build a quiz/worksheet FROM a lesson. ``ldd`` is the lesson as the teacher has
    it now (possibly edited and unsaved) — its objectives become the assessment's."""

    ldd: LessonDesignDocument
    spec: AssessmentSpec


class UnitAssessmentRequest(BaseModel):
    """Build a quiz/worksheet FROM a saved unit: ``days`` (1-based; omit for every
    day) picks which days' objectives it covers."""

    spec: AssessmentSpec
    days: list[int] | None = None


class RefineLessonRequest(BaseModel):
    """Reprompt one section of a lesson. ``ldd`` is the current (possibly already
    hand-edited) lesson; ``target`` names the section to improve (or ``"*"`` for
    the whole lesson); ``instruction`` is the teacher's free-text intent."""

    ldd: LessonDesignDocument
    target: str = Field(examples=["engagement_hook"])
    instruction: str = Field(min_length=1, examples=["make the hook about the local river"])


class SaveLessonRequest(GenerateRequest):
    """Generate a lesson AND persist it as a new document with its first version.
    Adds a title to the generation request; the owner is always the caller."""

    title: str | None = Field(default=None, max_length=300)


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


class VisibilityRequest(BaseModel):
    """Share a plan with the caller's school (read + copy only) or make it private."""

    visibility: Visibility


class CopyRequest(BaseModel):
    title: str | None = Field(default=None, max_length=300)


class CopiedDocument(BaseModel):
    document: StoredDocument
    version_id: str


class DocumentRefineResponse(BaseModel):
    """A refine outcome on a stored document; ``version_id`` is set only when the
    refine was committed as a new version."""

    result: RefineResult
    version_id: str | None = None


class GenerateUnitRequest(UnitRequest):
    """Plan-and-expand a multi-day unit AND persist it. Adds a title to the unit
    request (the owner is always the caller). When ``plan`` is supplied (a
    teacher-edited spine from ``/units/plan``) the planner is skipped and that exact
    arc is expanded, so edits to the spine actually drive generation."""

    title: str | None = Field(default=None, max_length=300)
    plan: UnitPlan | None = None


class PlanResponse(BaseModel):
    """The unit spine plus its syllabus-coverage report, so the preview can show
    whether the arc covers every chapter topic before the teacher expands it."""

    plan: UnitPlan
    coverage: CoverageReport


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
    records: list[dict[str, Any]] = Field(..., min_length=1, max_length=5000)
    space: str = Field(default=GLOBAL_SPACE, max_length=80,
                       description="knowledge space: 'global' or 'school:<id>'")


class CorpusDeleteRequest(BaseModel):
    ids: list[str] = Field(..., min_length=1, max_length=1000)


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
    text: str = Field(..., min_length=1, max_length=5_000_000)
    space: str = Field(default=GLOBAL_SPACE, max_length=80,
                       description="knowledge space: 'global' or 'school:<id>'")
    format: str = "markdown"
    source: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    chunker: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)


def format_errors(exc: ValidationError) -> list[dict[str, str]]:
    """Flatten pydantic errors into ``{loc, msg}`` the UI can pin to a field."""
    out: list[dict[str, str]] = []
    for e in exc.errors():
        loc = ".".join(str(p) for p in e["loc"])
        out.append({"loc": loc, "msg": e["msg"]})
    return out
