"""FastAPI dependency wiring: the container, authentication, the request context,
and permission checks.

- :func:`authenticate` resolves the caller: ``Authorization: Bearer`` first, then
  the ``lf_session`` cookie; neither → 401 ``auth.missing``. A cookie-authenticated
  state-changing request must carry ``X-LF-Request: 1`` (CSRF: a cross-site form
  cannot set custom headers; Bearer requests are not exposed to CSRF).
- :func:`request_ctx` binds the :class:`~lessonforge.request_context.RequestContext`
  (principal, knowledge scope from the SIGNED school, user context) for everything
  below the route — once per request (FastAPI caches dependencies per request).
- :func:`require` declares a permission on a router or route (D6).

Tests override :func:`authenticate` (there is no auth-off switch in production
code) and :func:`get_container`.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from functools import lru_cache
from typing import Any

from fastapi import Depends, Request
from starlette.concurrency import run_in_threadpool

from .. import request_context
from ..auth.principal import Perm, Principal
from ..container import Container
from ..domain.profile import TeacherProfile
from ..request_context import RequestContext
from .errors import ApiError

CSRF_HEADER = "x-lf-request"
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


@lru_cache(maxsize=1)
def get_container() -> Container:
    return Container.from_settings()


def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() == "bearer" and token.strip():
        return token.strip()
    return None


async def authenticate(request: Request, c: Container = Depends(get_container)) -> Principal:
    auth = c.platform.auth
    token = _bearer(request)
    if token is not None:
        principal = await run_in_threadpool(auth.from_bearer, token)
        request.state.auth_channel = "api"
        return principal
    cookie = request.cookies.get(c.settings.auth.session.cookie)
    if cookie:
        if request.method not in _SAFE_METHODS and request.headers.get(CSRF_HEADER) != "1":
            raise ApiError(403, "perm.denied", f"missing {CSRF_HEADER.upper()} header (CSRF)")
        principal = await run_in_threadpool(auth.from_session, cookie)
        request.state.auth_channel = "session"
        return principal
    raise ApiError(401, "auth.missing", "authentication required",
                   {"login_url": c.settings.auth.login_url} if c.settings.auth.login_url else None,
                   headers={"WWW-Authenticate": "Bearer"})


async def request_ctx(request: Request, principal: Principal = Depends(authenticate),
                      c: Container = Depends(get_container)) -> RequestContext:
    """Bind the request context in THIS task; Starlette copies it into the
    threadpool that runs sync routes and streaming generators."""
    ctx = RequestContext(
        request_id=getattr(request.state, "request_id", None) or RequestContext().request_id,
        principal=principal,
        access_scope=c.platform.knowledge_policy.scope_for(principal),
    )
    ctx.user_context = await run_in_threadpool(c.platform.context.context_for, principal)
    request_context.bind(ctx)
    return ctx


async def current_principal(ctx: RequestContext = Depends(request_ctx)) -> Principal:
    assert ctx.principal is not None
    return ctx.principal


def require(perm: Perm) -> Callable[..., Coroutine[Any, Any, Principal]]:
    async def _check(p: Principal = Depends(current_principal)) -> Principal:
        if not p.can(perm):
            raise ApiError(403, "perm.denied", f"requires the {perm.value!r} permission")
        return p

    _check.__name__ = f"require_{perm.value.replace('.', '_')}"
    return _check


def teacher_profile(c: Container, ctx: RequestContext) -> TeacherProfile:
    """The caller's saved profile, with UNSET defaults filled from their Edboox
    context (assignments, language) — explicit request fields still win later."""
    assert ctx.principal is not None
    profile = c.profile_store.load(ctx.principal.id)
    if ctx.user_context is not None:
        profile = c.platform.scope_policy.profile_defaults(profile, ctx.user_context)
    return profile
