"""The QA chatbot: conversations, streaming answers, interactive quizzes.
Requires ``chat``.

Every conversation, artifact and attempt is read through its owner (the caller):
another user's id is "not found". Retrieval scope follows ScopePolicy — pre-filled
at creation (``default``) or enforced every turn (``locked``) — and only the skills
the caller's permissions allow are ever offered to the intent router.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Body, Depends, Query
from fastapi.responses import Response, StreamingResponse

from ...activity import events as act
from ...auth.principal import Perm, Principal
from ...container import Container
from ...domain.artifacts import Quiz
from ...domain.chat import (
    ArtifactAttempt,
    ChatArtifact,
    ChatMessageRequest,
    Conversation,
    CreateConversationRequest,
    LearnInputRequest,
    StartLearningRequest,
    UpdateConversationRequest,
)
from ...export import ArtifactKind
from ...pagination import DEFAULT_LIMIT, MAX_LIMIT, Keyset, PageOut
from ...request_context import RequestContext
from ...services.chat.events import ChatEvent
from ...services.chat.quiz_play import play_view
from ..activity import emit
from ..deps import current_principal, get_container, request_ctx, require
from ..errors import not_found, unprocessable
from ..helpers import file_response, sse

log = logging.getLogger("lessonforge.api.chat")

router = APIRouter(prefix="/api/v1/chat", tags=["chat"],
                   dependencies=[Depends(require(Perm.CHAT))])


def _conversation(c: Container, conversation_id: str, p: Principal) -> Conversation:
    conv = c.get_chat_store().get(conversation_id, owner_id=p.id)
    if conv is None:
        raise not_found("conversation")
    return conv


def _artifact(c: Container, artifact_id: str, p: Principal) -> ChatArtifact:
    art = c.get_chat_store().get_artifact(artifact_id, owner_id=p.id)
    if art is None:
        raise not_found("artifact")
    return art


def _attempt(c: Container, attempt_id: str, p: Principal) -> tuple[ArtifactAttempt, ChatArtifact]:
    att = c.get_chat_store().get_attempt(attempt_id, owner_id=p.id)
    if att is None:
        raise not_found("attempt")
    return att, _artifact(c, att.artifact_id, p)


def _allowed_skills(c: Container, p: Principal) -> frozenset[str]:
    pipeline = c.get_chat_pipeline()
    return frozenset(n for n, s in pipeline.skills.items() if p.can(s.spec.requires))


@router.get("/config")
def chat_config(c: Container = Depends(get_container),
                ctx: RequestContext = Depends(request_ctx)) -> dict[str, object]:
    """What the chat UI needs to render its controls, including the caller's scope
    mode and the defaults their context pre-fills."""
    r = c.settings.chat.retrieval
    assert ctx.principal is not None
    mode = c.platform.scope_policy.mode_for(ctx.principal)
    bound = (c.platform.scope_policy.bound_filters(ctx.user_context)
             if ctx.user_context is not None and mode != "off" else {})
    return {"collections": r.collections, "filter_fields": r.filter_fields,
            "default_mode": r.default_mode, "scope_mode": mode, "scope_defaults": bound}


@router.get("/conversations", response_model=PageOut[Conversation])
def list_conversations(limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
                       cursor: str | None = Query(None, max_length=512),
                       c: Container = Depends(get_container),
                       p: Principal = Depends(current_principal)) -> PageOut[Conversation]:
    """The caller's conversations, most recently updated first."""
    page = c.get_chat_store().list(p.id, limit=limit, cursor=Keyset.decode(cursor))
    return PageOut(items=page.items, next_cursor=page.next_cursor)


@router.post("/conversations", response_model=Conversation)
def create_conversation(req: CreateConversationRequest, c: Container = Depends(get_container),
                        ctx: RequestContext = Depends(request_ctx)) -> Conversation:
    """Start a conversation. Its default retrieval scope is pre-filled (or locked)
    from the caller's context by ScopePolicy."""
    p = ctx.principal
    assert p is not None and ctx.user_context is not None
    policy = c.platform.scope_policy
    defaults = policy.conversation_defaults(p, ctx.user_context, req.defaults)
    conv = c.get_chat_store().create(Conversation(owner_id=p.id, title=req.title or "New chat",
                                                  defaults=defaults))
    emit(c, "chat.conversation.created", act.ConversationCreated(
        scope_mode=policy.mode_for(p), grade=defaults.grade,
        subject=defaults.subject[:160] if defaults.subject else None),
        resource_type="conversation", resource_id=conv.id)
    return conv


@router.get("/conversations/{conversation_id}")
def get_conversation(conversation_id: str, c: Container = Depends(get_container),
                     p: Principal = Depends(current_principal)) -> dict[str, object]:
    """A conversation with its full history and the artifacts its turns produced
    (attempt summaries loaded in one query)."""
    conv = _conversation(c, conversation_id, p)
    msgs = c.get_chat_store().messages(conversation_id)
    return {"conversation": conv.model_dump(by_alias=True),
            "messages": [m.model_dump() for m in msgs],
            "artifacts": c.get_artifact_service().summaries(conversation_id)}


@router.patch("/conversations/{conversation_id}", response_model=Conversation)
def update_conversation(conversation_id: str, req: UpdateConversationRequest,
                        c: Container = Depends(get_container),
                        ctx: RequestContext = Depends(request_ctx)) -> Conversation:
    """Rename and/or change the default scope (still constrained by ScopePolicy)."""
    p = ctx.principal
    assert p is not None and ctx.user_context is not None
    defaults = (c.platform.scope_policy.conversation_defaults(p, ctx.user_context, req.defaults)
                if req.defaults is not None else None)
    updated = c.get_chat_store().update(conversation_id, owner_id=p.id, title=req.title,
                                        defaults=defaults)
    if updated is None:
        raise not_found("conversation")
    return updated


@router.delete("/conversations/{conversation_id}")
def delete_conversation(conversation_id: str, c: Container = Depends(get_container),
                        p: Principal = Depends(current_principal)) -> dict[str, bool]:
    if not c.get_chat_store().delete(conversation_id, owner_id=p.id):
        raise not_found("conversation")
    return {"deleted": True}


@router.post("/conversations/{conversation_id}/message")
def send_message(conversation_id: str, req: ChatMessageRequest,
                 c: Container = Depends(get_container),
                 ctx: RequestContext = Depends(request_ctx)) -> StreamingResponse:
    """Ask a question and stream the answer as Server-Sent Events (see
    docs/CHAT_EVENTS.md). ``mode``/``filters`` override the conversation's default
    scope for this message — unless the caller's scope is ``locked``."""
    p = ctx.principal
    assert p is not None and ctx.user_context is not None
    conv = _conversation(c, conversation_id, p)
    pipeline = c.get_chat_pipeline()
    turn = c.platform.scope_policy.turn_scope(p, ctx.user_context)
    allowed = _allowed_skills(c, p)

    return _stream(pipeline.stream(conv, req, turn=turn, allowed_skills=allowed,
                                   language=_language(ctx)), ctx)


def _language(ctx: RequestContext) -> str:
    uc = ctx.user_context
    lang = (uc.student.medium if uc and uc.student and uc.student.medium else None) or \
        (uc.language if uc else None)
    return lang or "en"


def _stream(events, ctx: RequestContext) -> StreamingResponse:
    def event_stream():
        try:
            for event in events:
                yield sse(event)
        except Exception:   # never leak internals into the stream
            log.exception("chat turn failed (request %s)", ctx.request_id)
            yield sse(ChatEvent("error", {"code": "internal",
                                          "message": "the assistant could not answer",
                                          "request_id": ctx.request_id}))

    return StreamingResponse(event_stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ── learn mode (docs/LEARN_MODE.md) ──────────────────────────────────────────
def _learn(c: Container):
    learn = c.get_chat_pipeline().learn
    if learn is None:
        raise not_found("learn mode")
    return learn


@router.post("/conversations/{conversation_id}/learning-sessions")
def start_learning(conversation_id: str, req: StartLearningRequest,
                   c: Container = Depends(get_container),
                   ctx: RequestContext = Depends(request_ctx)) -> StreamingResponse:
    """Start a scaffolded learn-mode session on ``topic`` and stream its opening turn
    (SSE: ``status`` · ``token`` · ``learn_widget`` · ``learn_state`` · ``done``). A
    session already running in the conversation is paused, not lost."""
    p = ctx.principal
    assert p is not None and ctx.user_context is not None
    conv = _conversation(c, conversation_id, p)
    _learn(c)
    turn = c.platform.scope_policy.turn_scope(p, ctx.user_context)
    return _stream(c.get_chat_pipeline().start_learning(conv, req, turn=turn,
                                                        language=_language(ctx)), ctx)


@router.post("/conversations/{conversation_id}/learn")
def learn_input(conversation_id: str, req: LearnInputRequest,
                c: Container = Depends(get_container),
                ctx: RequestContext = Depends(request_ctx)) -> StreamingResponse:
    """Answer the learn-mode widget on screen, or press a quick action (hint, example,
    skip, faster, slower, pause, end, choose, resume). Typed replies go to ``/message``."""
    p = ctx.principal
    assert p is not None
    conv = _conversation(c, conversation_id, p)
    _learn(c)
    return _stream(c.get_chat_pipeline().learn_input(conv, req), ctx)


@router.get("/conversations/{conversation_id}/learning-session")
def current_learning_session(conversation_id: str, c: Container = Depends(get_container),
                             p: Principal = Depends(current_principal)) -> dict[str, Any]:
    """The conversation's active or paused session (student-safe view), or ``null``."""
    conv = _conversation(c, conversation_id, p)
    session = _learn(c).current(conv.id)
    out: dict[str, Any] = {"session": session.public() if session else None}
    if session and session.pending:   # re-show the widget awaiting an answer after a reload
        found = session.find_check(session.pending.check_id)
        if found:
            from ...services.learn.widgets import WidgetService

            out["widget"] = WidgetService.play(found[1], purpose=session.pending.purpose,
                                               objective_id=found[0].id,
                                               instance_id=session.pending.instance_id)
    return out


@router.post("/learning-sessions/{session_id}/end")
def end_learning_session(session_id: str, c: Container = Depends(get_container),
                         p: Principal = Depends(current_principal)) -> dict[str, Any]:
    learn = _learn(c)
    session = learn.store.get(session_id, owner_id=p.id)
    if session is None:
        raise not_found("learning session")
    if session.status in ("planning", "active", "paused"):
        session = learn.end(session)
    return session.public()


# ── chat artifacts: interactive quizzes, played & revisited ──────────────────
@router.get("/artifacts/{artifact_id}")
def get_chat_artifact(artifact_id: str, c: Container = Depends(get_container),
                      p: Principal = Depends(current_principal)):
    """The artifact as the PLAYER sees it — questions and choices, never answers."""
    art = _artifact(c, artifact_id, p)
    svc = c.get_artifact_service().for_kind(art.kind)
    return {"summary": svc.summary(art), "play": play_view(art), "attempts": svc.list_attempts(art)}


@router.delete("/artifacts/{artifact_id}")
def delete_chat_artifact(artifact_id: str, c: Container = Depends(get_container),
                         p: Principal = Depends(current_principal)) -> dict[str, bool]:
    if not c.get_chat_store().delete_artifact(artifact_id, owner_id=p.id):
        raise not_found("artifact")
    return {"deleted": True}


@router.post("/artifacts/{artifact_id}/attempts")
def start_attempt(artifact_id: str, c: Container = Depends(get_container),
                  p: Principal = Depends(current_principal)):
    """Begin a new play-through (a quiz can be retaken any number of times)."""
    art = _artifact(c, artifact_id, p)
    att = c.get_artifact_service().for_kind(art.kind).start(art, owner_id=p.id)
    return {"attempt_id": att.id, "total": att.total}


@router.post("/attempts/{attempt_id}/answer")
def answer_question(attempt_id: str, payload: dict[str, Any] = Body(...),
                    c: Container = Depends(get_container),
                    p: Principal = Depends(current_principal)):
    """Grade one answer on the server and reveal the right answer + explanation."""
    att, art = _attempt(c, attempt_id, p)
    qid = str(payload.get("question_id", "")).strip()
    if not qid:
        raise unprocessable("`question_id` is required")
    try:
        return c.get_artifact_service().for_kind(art.kind).answer(
            art, att, qid, payload.get("response"), payload.get("self_mark"))
    except KeyError:
        raise unprocessable(f"unknown question {qid!r}") from None


@router.post("/attempts/{attempt_id}/finish")
def finish_attempt(attempt_id: str, c: Container = Depends(get_container),
                   p: Principal = Depends(current_principal)):
    """End the attempt now (unanswered = missed) and return the full review."""
    att, art = _attempt(c, attempt_id, p)
    svc = c.get_artifact_service().for_kind(art.kind)
    return svc.review(art, svc.finish(art, att))


@router.get("/attempts/{attempt_id}")
def get_attempt_review(attempt_id: str, c: Container = Depends(get_container),
                       p: Principal = Depends(current_principal)):
    att, art = _attempt(c, attempt_id, p)
    return c.get_artifact_service().for_kind(art.kind).review(art, att)


@router.post("/attempts/{attempt_id}/retry-missed")
def retry_missed(attempt_id: str, c: Container = Depends(get_container),
                 p: Principal = Depends(current_principal)):
    """A new quiz of just the questions this attempt got wrong (no LLM)."""
    att, art = _attempt(c, attempt_id, p)
    svc = c.get_artifact_service().for_kind(art.kind)
    try:
        new = svc.retry_missed(art, att)
    except ValueError as exc:
        raise unprocessable(str(exc)) from exc
    return svc.summary(new)


@router.post("/artifacts/{artifact_id}/export")
def export_chat_quiz(artifact_id: str, fmt: str = Query(default="docx", max_length=16),
                     c: Container = Depends(get_container),
                     p: Principal = Depends(current_principal)) -> Response:
    """Download the quiz as a printable file."""
    art = _artifact(c, artifact_id, p)
    if art.kind != "quiz":
        raise unprocessable("only quizzes can be exported")
    try:
        rendered = c.exporter.render_artifact(ArtifactKind.quiz, Quiz.model_validate(art.payload),
                                              fmt=fmt)
    except ValueError as exc:
        raise unprocessable(str(exc)) from exc
    emit(c, "artifact.exported", act.ArtifactExported(artifact="quiz", format=fmt),
         resource_type="quiz", resource_id=art.id)
    return file_response(rendered)
