"""Platform routes: health, ``/me``, the launch / logout flow, and the static
page shells.

Pages (``/``, ``/chat``, ``/units``, ``/library``, ``/corpus``) are static shells:
they embed no data and call only ``/api/v1``, so the API is the whole contract
and Edboox's own UI can replace ours screen by screen (D3).
"""

from __future__ import annotations

import time
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from ... import request_context
from ...activity import events as act
from ...auth.principal import Perm, Principal
from ...auth.service import TTLCache
from ...container import Container
from ...request_context import RequestContext
from ..activity import emit
from ..deps import get_container, request_ctx, require
from ..errors import ApiError

_STATIC = Path(__file__).resolve().parent.parent / "static"
_MAX_FORM_BYTES = 16 * 1024
# first bearer call of the day per principal (per process) → session.started
_api_sessions: TTLCache[tuple[str, str], bool] = TTLCache(ttl_s=24 * 3600, max_items=100_000)

pages = APIRouter(include_in_schema=False)
ops = APIRouter(tags=["ops"])
api = APIRouter(prefix="/api/v1")


# ── pages ────────────────────────────────────────────────────────────────────
def _page(name: str, title: str) -> HTMLResponse:
    f = _STATIC / name
    html = f.read_text(encoding="utf-8") if f.exists() else (
        f"<h1>{title}</h1><p>UI asset missing. See /docs for the API.</p>")
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


@pages.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return _page("index.html", "LessonForge")


@pages.get("/units", response_class=HTMLResponse)
def units_page() -> HTMLResponse:
    return _page("units.html", "Unit planner")


@pages.get("/library", response_class=HTMLResponse)
def library_page() -> HTMLResponse:
    return _page("library.html", "Library")


@pages.get("/corpus", response_class=HTMLResponse)
def corpus_page() -> HTMLResponse:
    return _page("corpus.html", "Corpus manager")


@pages.get("/chat", response_class=HTMLResponse)
def chat_page() -> HTMLResponse:
    return _page("chat.html", "LessonForge chat")


@pages.get("/admin", response_class=HTMLResponse)
def admin_page() -> HTMLResponse:
    return _page("admin.html", "Owner console")


# ── launch / logout (the LTI-style flow our UI rides on) ─────────────────────
@pages.post("/auth/launch")
async def launch(request: Request, c: Container = Depends(get_container)) -> Response:
    """Edboox auto-POSTs a one-time, ≤60 s token here (form field ``token``; in the
    body, never the URL). We verify it, burn its ``jti``, upsert the principal,
    start a context refresh, set the session cookie and redirect into the app."""
    body = await request.body()
    if len(body) > _MAX_FORM_BYTES:
        raise ApiError(413, "validation", "launch form too large")
    token = (parse_qs(body.decode("utf-8", "replace")).get("token") or [""])[0].strip()
    if not token:
        raise ApiError(401, "auth.missing", "launch token missing")
    platform = c.platform
    result = await run_in_threadpool(platform.auth.launch, token)
    p = result.principal
    platform.context.refresh_async(p)
    with request_context.bound(RequestContext(principal=p,
                                              access_scope=platform.knowledge_policy.scope_for(p))):
        emit(c, "session.started", act.SessionStarted(channel="launch"))
    session = c.settings.auth.session
    target = "/" if p.can(Perm.AUTHOR) else "/chat"
    resp = RedirectResponse(target, status_code=303)
    resp.set_cookie(session.cookie, result.session, max_age=session.ttl_min * 60, httponly=True,
                    secure=session.secure, samesite="lax", path="/")
    return resp


@pages.get("/auth/launch")
def launch_revisited() -> Response:
    """Back/refresh onto the launch URL (a GET carries no token): continue into
    the app — its shell sends an unauthenticated user to Edboox to sign in."""
    return RedirectResponse("/", status_code=303)


@pages.post("/auth/logout")
def logout(c: Container = Depends(get_container)) -> Response:
    """Clear our session and send the user back to Edboox."""
    session = c.settings.auth.session
    resp = RedirectResponse(c.settings.auth.login_url or "/", status_code=303)
    resp.delete_cookie(session.cookie, path="/", secure=session.secure, httponly=True,
                       samesite="lax")
    return resp


# ── health ───────────────────────────────────────────────────────────────────
@ops.get("/health")
def health() -> dict[str, str]:
    """Liveness: the process is up (no dependencies touched)."""
    return {"status": "ok"}


@ops.get("/health/ready")
def ready(c: Container = Depends(get_container)) -> Response:
    """Readiness: the database (when used) answers. 503 otherwise."""
    db = c.platform.db
    ok = db is None or db.health()
    return JSONResponse({"status": "ok" if ok else "unavailable", "database": ok},
                        status_code=200 if ok else 503)


@api.get("/health/providers", tags=["ops"], dependencies=[Depends(require(Perm.OPS))])
def provider_health(c: Container = Depends(get_container)) -> dict[str, object]:
    """Which backend is wired for each component and whether it is reachable."""
    s = c.settings
    db = c.platform.db
    return {
        "llm": {"provider": s.llm.provider, "model": s.llm.model, "reachable": c.llm.health()},
        "embedding": {"provider": s.embedding.provider, "model": s.embedding.model},
        "reranker": {"provider": s.reranker.provider, "model": s.reranker.model},
        "vector_store": {"provider": s.vector_store.provider,
                         "reachable": c.vector_store.health()},
        "database": {"provider": "postgres" if db else "memory",
                     "reachable": db.health() if db else True},
    }


# ── me ───────────────────────────────────────────────────────────────────────
_PAGES = (("/", "Lesson editor", Perm.AUTHOR), ("/units", "Units", Perm.AUTHOR),
          ("/library", "Library", Perm.AUTHOR), ("/chat", "Chat", Perm.CHAT),
          ("/corpus", "Knowledge base", Perm.CORPUS_MANAGE), ("/admin", "Owner console", Perm.ADMIN))


@api.get("/me", tags=["me"])
def me(request: Request, c: Container = Depends(get_container),
       ctx: RequestContext = Depends(request_ctx)) -> dict[str, object]:
    """Who the caller is, what they may do, and their context summary — what a UI
    (ours or Edboox's) builds its navigation and pickers from. Requires only
    authentication."""
    p: Principal = ctx.principal  # type: ignore[assignment]
    _note_api_session(c, request, p)
    policy = c.platform.scope_policy
    return {
        "id": p.id, "subject": p.subject, "issuer": p.issuer, "name": p.display_name,
        "role": p.primary_role, "roles": sorted(p.roles),
        "permissions": sorted(perm.value for perm in p.permissions),
        "tenant_id": p.tenant_id,
        "context": ctx.user_context.summary() if ctx.user_context else None,
        "scope_mode": policy.mode_for(p),
        "knowledge_spaces": "*" if ctx.access_scope.unrestricted else sorted(ctx.access_scope.spaces),
        "nav": [{"path": path, "label": label} for path, label, perm in _PAGES if p.can(perm)],
        "logout_url": "/auth/logout",
    }


def _note_api_session(c: Container, request: Request, p: Principal) -> None:
    if getattr(request.state, "auth_channel", None) != "api":
        return
    key = (p.id, time.strftime("%Y-%m-%d", time.gmtime()))
    if _api_sessions.get(key) is None:
        _api_sessions.put(key, True)
        emit(c, "session.started", act.SessionStarted(channel="api"))

