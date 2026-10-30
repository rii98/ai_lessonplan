"""The activity-event catalogue (§15, documented in docs/ACTIVITY_EVENTS.md).

Each event type has a Pydantic *props* model. Unknown props are rejected and every
string is bounded and shape-checked, so message text, names or other free input
cannot be logged by accident — **the models are the privacy guarantee**. Adding an
event is a new props class + one registry line; changing one incompatibly bumps
its ``schema_ver``.

:func:`build_event` fills the actor (subject, role, school, grade) from the bound
:class:`~lessonforge.request_context.RequestContext` in one place, so call sites
only supply what is specific to their event.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from .. import request_context
from ..domain.vocabulary import load_vocabulary

# Identifier-shaped strings (intent names, collections, formats, kinds…).
Ident = Annotated[str, StringConstraints(max_length=64, pattern=r"^[A-Za-z0-9_.:\-]+$")]
# Curriculum labels (canonical subject, a book's chapter heading) — controlled
# vocabulary / corpus text, never user input; still bounded.
Label = Annotated[str, StringConstraints(max_length=160)]


class Props(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    @field_validator("subject", mode="before", check_fields=False)
    @classmethod
    def _canonical_subject(cls, v: Any) -> str | None:
        """A subject may originate from a user-chosen filter; only a canonical
        vocabulary subject is ever recorded — anything else is dropped, never logged."""
        return load_vocabulary().subject(v) if v not in (None, "") else None


class SessionStarted(Props):
    channel: Literal["launch", "api"]


class ConversationCreated(Props):
    scope_mode: Literal["off", "default", "locked"]
    grade: int | None = Field(default=None, ge=1, le=12)
    subject: Label | None = None


class ChatMessage(Props):
    intent: Ident
    intent_source: Ident | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    subject: Label | None = None
    chapters: list[Label] = Field(default_factory=list, max_length=20)
    collections: list[Ident] = Field(default_factory=list, max_length=10)
    spaces_hit: list[Ident] = Field(default_factory=list, max_length=10)
    citations: int = Field(default=0, ge=0)
    grounded: bool = False
    latency_ms: int = Field(default=0, ge=0)
    tokens_in: int = Field(default=0, ge=0)
    tokens_out: int = Field(default=0, ge=0)
    model: Annotated[str, StringConstraints(max_length=200)] | None = None


class QuizCreated(Props):
    source: Literal["chat", "lesson", "unit", "brief"]
    questions: int = Field(ge=0)
    question_types: list[Ident] = Field(default_factory=list, max_length=20)
    difficulty: Ident | None = None
    subject: Label | None = None
    chapter: Label | None = None


class QuizAttemptStarted(Props):
    quiz_id: str = Field(max_length=64)


class QuizAttemptFinished(Props):
    score: float = Field(ge=0)
    total: int = Field(ge=0)
    percent: float = Field(ge=0, le=100)
    duration_s: int = Field(ge=0)
    retry: bool = False


class Generated(Props):
    grade: int | None = Field(default=None, ge=1, le=12)
    subject: Label | None = None
    days: int | None = Field(default=None, ge=1)
    latency_ms: int = Field(default=0, ge=0)
    tokens_in: int = Field(default=0, ge=0)
    tokens_out: int = Field(default=0, ge=0)


class DocumentRefined(Props):
    section: Ident
    kind: Literal["ai", "manual"]


class DocumentVisibility(Props):
    kind: Literal["lesson", "unit"]
    grade: int | None = Field(default=None, ge=1, le=12)
    subject: Label | None = None


class DocumentCopied(DocumentVisibility):
    source_owner: Annotated[str, StringConstraints(max_length=128)] | None = None


class ArtifactExported(Props):
    artifact: Ident
    format: Ident


class CorpusIngested(Props):
    collection: Ident
    space: Ident
    documents: int = Field(ge=0)
    chunks: int = Field(ge=0)


# type → (props model, schema version)
CATALOGUE: dict[str, tuple[type[Props], int]] = {
    "session.started": (SessionStarted, 1),
    "chat.conversation.created": (ConversationCreated, 1),
    "chat.message": (ChatMessage, 1),
    "quiz.created": (QuizCreated, 1),
    "quiz.attempt.started": (QuizAttemptStarted, 1),
    "quiz.attempt.finished": (QuizAttemptFinished, 1),
    "lesson.generated": (Generated, 1),
    "unit.generated": (Generated, 1),
    "document.refined": (DocumentRefined, 1),
    "document.shared": (DocumentVisibility, 1),
    "document.unshared": (DocumentVisibility, 1),
    "document.copied": (DocumentCopied, 1),
    "artifact.exported": (ArtifactExported, 1),
    "corpus.ingested": (CorpusIngested, 1),
}

ResourceType = Literal["conversation", "quiz", "attempt", "document", "corpus"]


@dataclass(frozen=True, slots=True)
class ActivityEvent:
    type: str
    props: dict[str, Any]
    role: str
    schema_ver: int = 1
    principal_id: str | None = None
    subject: str | None = None
    tenant_id: str | None = None
    grade: int | None = None
    resource_type: str | None = None
    resource_id: str | None = None
    request_id: str | None = None
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))


def build_event(type_: str, props: Props, *, resource_type: ResourceType | None = None,
                resource_id: str | None = None) -> ActivityEvent | None:
    """An event for ``type_`` with the actor taken from the request context.
    Returns ``None`` (nothing to record) when no authenticated request is bound."""
    model, ver = CATALOGUE[type_]
    if not isinstance(props, model):
        raise TypeError(f"{type_} takes {model.__name__} props, got {type(props).__name__}")
    ctx = request_context.current()
    if ctx is None or ctx.principal is None:
        return None
    p = ctx.principal
    grade = None
    if ctx.user_context is not None and ctx.user_context.student is not None:
        grade = ctx.user_context.student.grade
    return ActivityEvent(
        type=type_, schema_ver=ver, props=props.model_dump(mode="json", exclude_none=True),
        role=p.primary_role, principal_id=p.id, subject=p.subject, tenant_id=p.tenant_id,
        grade=grade if grade is not None else p.grade,
        resource_type=resource_type, resource_id=resource_id, request_id=ctx.request_id,
    )
