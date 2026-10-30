"""Live sign-in demo (``/api/v1/demo``) — OFF by default (``demo.enabled``).

Lets the fake Edboox's ``/demo`` page show, step by step, what really happens when
someone opens LessonForge from Edboox. Nothing here re-implements security: the
launch step calls the very same :meth:`AuthService.launch` as ``/auth/launch``, and
the trace only *annotates* what that real code did (which key, which checks,
which principal, which shelves). When disabled every route is a 404, and the
routes are excluded from the published API contract.
"""

from __future__ import annotations

import time
from typing import Any

import jwt
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from ...auth.principal import Perm
from ...auth.verifier import AuthError
from ...container import Container
from ...request_context import RequestContext
from ..deps import get_container, request_ctx
from ..errors import ApiError

router = APIRouter(prefix="/api/v1/demo", include_in_schema=False)


def _enabled(c: Container = Depends(get_container)) -> Container:
    if not c.settings.demo.enabled:
        raise ApiError(404, "not_found", "Not Found")
    return c


class LaunchRequest(BaseModel):
    token: str = Field(..., max_length=8192)


@router.get("/status")
def status(c: Container = Depends(_enabled)) -> dict[str, Any]:
    keys = c.platform.verifier.key_status() if c.settings.auth.issuers else []
    return {"enabled": True, "issuers": keys,
            "launch_max_age_s": c.settings.auth.launch_max_age_s,
            "session_cookie": c.settings.auth.session.cookie}


def _step(title: str, ok: bool, detail: dict[str, Any], explain: str) -> dict[str, Any]:
    return {"title": title, "ok": ok, "detail": detail, "explain": explain}


@router.post("/launch")
async def launch(req: LaunchRequest, c: Container = Depends(_enabled)) -> JSONResponse:
    """Run the REAL launch and return a trace of it (and the real session cookie)."""
    steps: list[dict[str, Any]] = []
    try:
        header = jwt.get_unverified_header(req.token)
        claims = jwt.decode(req.token, options={"verify_signature": False})
    except jwt.InvalidTokenError:
        raise ApiError(422, "validation", "that is not a JWT") from None
    now = time.time()
    steps.append(_step("Read the pass (not trusted yet)", True, {
        "header": header, "claims": claims,
        "age_s": round(now - float(claims.get("iat", now))),
        "expires_in_s": round(float(claims.get("exp", now)) - now)},
        "Anyone can read a JWT; only the signature makes it trustworthy."))

    issuer = next((i for i in c.settings.auth.issuers if i.issuer == claims.get("iss")), None)
    steps.append(_step("Find the issuer in our config", issuer is not None, {
        "iss": claims.get("iss"),
        "matched": issuer.name if issuer else None,
        "allowed_algorithms": issuer.algorithms if issuer else [],
        "role_map": issuer.role_map if issuer else {}},
        "Only issuers listed in config are trusted; the token's own 'alg' is never trusted."))

    keys = next((k for k in c.platform.verifier.key_status()
                 if issuer and k["issuer"] == issuer.issuer), {})
    steps.append(_step("Fetch Edboox's PUBLIC keys (JWKS)", bool(keys.get("ok")), {
        **keys, "token_kid": header.get("kid"),
        "kid_known": header.get("kid") in keys.get("kids", [])},
        "Public keys can verify a signature but can never create one."))

    before = c.platform.directory.find(issuer.name, str(claims.get("sub"))) if issuer else None
    try:
        result = await run_in_threadpool(c.platform.auth.launch, req.token)
    except AuthError as exc:
        steps.append(_step("Verify the signature and every check", False,
                           {"code": exc.code, "message": exc.message},
                           "The real /auth/launch code refused this pass."))
        return JSONResponse({"ok": False, "steps": steps}, status_code=200)

    p, vt = result.principal, result.token
    steps.append(_step("Verify the signature and every check", True, {
        "signature": f"valid {header.get('alg')} signature by key {header.get('kid')}",
        "issuer": vt.issuer, "audience": "lessonforge",
        "not_expired": True, "age_ok": f"≤ {c.settings.auth.launch_max_age_s}s",
        "jti_first_use": f"{vt.jti} recorded — this pass can never be used again"},
        "This is the production verifier; any failure would stop here."))
    steps.append(_step("Who is this in LessonForge?", True, {
        "principal_id": p.id, "edboox_identity": f"{p.issuer}:{p.subject}",
        "new_here": before is None, "role": p.primary_role, "roles": sorted(p.roles),
        "permissions": sorted(x.value for x in p.permissions),
        "school (signed)": p.tenant_id},
        "Edboox says WHO; LessonForge's own role map decides WHAT. Superadmin is never "
        "taken from the token."))
    scope = c.platform.knowledge_policy.scope_for(p)
    steps.append(_step("Which knowledge shelves may they read?", True, {
        "spaces": "*" if scope.unrestricted else sorted(scope.spaces)},
        "Decided only from the signed school id — never from cached details."))
    session = c.settings.auth.session
    target = "/" if p.can(Perm.AUTHOR) else "/chat"
    steps.append(_step("Start a LessonForge session", True, {
        "cookie": session.cookie, "httpOnly": True, "sameSite": "Lax",
        "secure": session.secure, "lifetime_min": session.ttl_min,
        "contains": "only the principal id, signed with LessonForge's own secret",
        "lands_on": target},
        "From now on the browser carries this cookie; the Edboox pass is spent."))
    from ... import request_context
    from ...activity import events as act
    from ..activity import emit

    with request_context.bound(RequestContext(principal=p, access_scope=scope)):
        emit(c, "session.started", act.SessionStarted(channel="launch"))   # parity with /auth/launch
    resp = JSONResponse({"ok": True, "steps": steps, "principal_id": p.id, "lands_on": target})
    resp.set_cookie(session.cookie, result.session, max_age=session.ttl_min * 60, httponly=True,
                    secure=session.secure, samesite="lax", path="/")
    return resp


@router.post("/context")
def context(request: Request, c: Container = Depends(_enabled),
            ctx: RequestContext = Depends(request_ctx)) -> dict[str, Any]:
    """Pull the caller's details from Edboox NOW and show what we kept."""
    assert ctx.principal is not None
    before = ctx.user_context.model_dump(mode="json") if ctx.user_context else None
    after = c.platform.context.refresh(ctx.principal)
    return {"provider": c.settings.context.provider, "before": before,
            "after": after.model_dump(mode="json") if after else None,
            "prompt_facts": after.prompt_facts() if after else None}
