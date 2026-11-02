"""FastAPI application factory.

The public, versioned contract lives under ``/api/v1`` (routers in
``api/routers``); page routes serve static shells only. Cross-cutting concerns
are wired here, once:

- a pure ASGI middleware assigns every request an id (honouring a sane incoming
  ``X-Request-ID``), echoes it, and sets security headers — pure ASGI so it never
  buffers a streaming response or breaks context propagation;
- CORS is an explicit origin allowlist (never ``*`` with credentials);
- every error is the uniform envelope (``api/errors.py``);
- startup validates the access configuration, required secrets and the database
  schema, and refuses to serve if any is wrong.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..config import load_settings
from . import errors, webui
from .deps import get_container
from .routers import admin, artifacts, chat, corpus, demo, documents, lessons, platform, reports

log = logging.getLogger("lessonforge.api")

_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{8,64}$")


class RequestIdMiddleware:
    """Request id + security headers, as a pure ASGI middleware."""

    def __init__(self, app: ASGIApp, *, frame_ancestors: str = "'self'") -> None:
        self.app = app
        self._headers = [
            (b"x-content-type-options", b"nosniff"),
            (b"referrer-policy", b"same-origin"),
            (b"content-security-policy", f"frame-ancestors {frame_ancestors}".encode()),
        ]

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        incoming = dict(scope.get("headers") or []).get(b"x-request-id", b"").decode("latin-1")
        rid = incoming if _REQUEST_ID.match(incoming) else uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = rid

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {k.lower() for k, _ in headers}
                headers.append((b"x-request-id", rid.encode()))
                headers.extend(h for h in self._headers if h[0] not in present)
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    factory = app.dependency_overrides.get(get_container, get_container)
    container = factory()
    container.platform.validate()   # raises → the process does not start
    container.warm()
    log.info("LessonForge API ready")
    try:
        yield
    finally:
        container.platform.close()


def create_app() -> FastAPI:
    settings = load_settings()
    origins = [o for o in settings.api.cors_origins if o]
    if "*" in origins:
        raise ValueError("api.cors_origins may not contain '*' (credentials are allowed)")

    app = FastAPI(
        title="LessonForge — AI Lesson Plan Creator",
        version="1.0.0",
        summary="AI that thinks like an experienced teacher.",
        lifespan=_lifespan,
    )
    errors.install(app, login_url=settings.auth.login_url)
    if origins:
        app.add_middleware(
            CORSMiddleware, allow_origins=origins, allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
            allow_headers=["Authorization", "Content-Type", "X-LF-Request", "X-Request-ID"],
            expose_headers=["X-Request-ID", "Content-Disposition"],
        )
    app.add_middleware(RequestIdMiddleware,
                       frame_ancestors=" ".join(["'self'", *origins]))

    # The web client (web/ → api/webui or $LF_WEB_DIST): hashed bundles + app shell.
    app.mount("/assets", webui.assets_app(), name="assets")

    for router in (platform.pages, platform.ops, platform.api, lessons.router,
                   documents.router, artifacts.router, corpus.router, chat.router,
                   reports.router, admin.router, demo.router, webui.router):
        app.include_router(router)
    return app


app = create_app()
