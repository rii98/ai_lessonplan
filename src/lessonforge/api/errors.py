"""The uniform error envelope (API contract rule 5).

Every non-2xx response is::

    {"error": {"code": "perm.denied", "message": "…", "details": …, "request_id": "…"}}

``code`` is stable and machine-readable — UIs branch on it, never on text:
``auth.missing``, ``auth.expired``, ``auth.invalid``, ``perm.denied``,
``not_found``, ``conflict``, ``validation``, ``upstream.unavailable``,
``method_not_allowed``, ``internal``. A 500 never echoes the exception text (it
is logged with the request id instead).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..auth.verifier import AuthError
from ..db.pool import DatabaseUnavailable
from ..pagination import InvalidCursor
from ..providers.base import UpstreamUnavailable
from ..services.documents.service import DocumentConflict, DocumentForbidden, DocumentNotFound

log = logging.getLogger("lessonforge.api")

_STATUS_CODES = {400: "validation", 401: "auth.missing", 403: "perm.denied", 404: "not_found",
                 405: "method_not_allowed", 409: "conflict", 413: "validation",
                 422: "validation", 503: "upstream.unavailable"}


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, details: Any = None,
                 headers: dict[str, str] | None = None) -> None:
        super().__init__(message)
        self.status, self.code, self.message, self.details = status, code, message, details
        self.headers = headers


def not_found(what: str = "resource") -> ApiError:
    return ApiError(404, "not_found", f"{what} not found")


def unprocessable(message: str, details: Any = None) -> ApiError:
    return ApiError(422, "validation", message, details)


def envelope(request: Request, status: int, code: str, message: str, details: Any = None,
             headers: dict[str, str] | None = None) -> JSONResponse:
    body: dict[str, Any] = {"code": code, "message": message}
    if details is not None:
        body["details"] = details
    rid = getattr(request.state, "request_id", None)
    if rid:
        body["request_id"] = rid
    return JSONResponse({"error": body}, status_code=status, headers=headers)


def _validation_details(exc: RequestValidationError) -> list[dict[str, str]]:
    return [{"loc": ".".join(str(p) for p in e.get("loc", ())), "msg": str(e.get("msg", "")),
             "type": str(e.get("type", ""))} for e in exc.errors()]


def install(app: FastAPI, *, login_url: str = "") -> None:
    @app.exception_handler(ApiError)
    async def _api(request: Request, exc: ApiError):
        return envelope(request, exc.status, exc.code, exc.message, exc.details, exc.headers)

    @app.exception_handler(AuthError)
    async def _auth(request: Request, exc: AuthError):
        details = {"login_url": login_url} if exc.status == 401 and login_url else None
        headers = {"WWW-Authenticate": "Bearer"} if exc.status == 401 else None
        return envelope(request, exc.status, exc.code, exc.message, details, headers)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        return envelope(request, 422, "validation", "request validation failed",
                        _validation_details(exc))

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException):
        code = _STATUS_CODES.get(exc.status_code, "internal" if exc.status_code >= 500 else "error")
        message = exc.detail if isinstance(exc.detail, str) else code
        details = exc.detail if not isinstance(exc.detail, str) else None
        return envelope(request, exc.status_code, code, message, details,
                        getattr(exc, "headers", None))

    @app.exception_handler(DocumentNotFound)
    async def _doc404(request: Request, exc: DocumentNotFound):
        return envelope(request, 404, "not_found", "document not found")

    @app.exception_handler(DocumentForbidden)
    async def _doc403(request: Request, exc: DocumentForbidden):
        return envelope(request, 403, "perm.denied", str(exc))

    @app.exception_handler(DocumentConflict)
    async def _doc409(request: Request, exc: DocumentConflict):
        return envelope(request, 409, "conflict", str(exc))

    @app.exception_handler(InvalidCursor)
    async def _cursor(request: Request, exc: InvalidCursor):
        return envelope(request, 422, "validation", "invalid cursor")

    @app.exception_handler(DatabaseUnavailable)
    async def _db(request: Request, exc: DatabaseUnavailable):
        log.error("database unavailable (request %s): %s",
                  getattr(request.state, "request_id", "-"), exc)
        return envelope(request, 503, "upstream.unavailable", "database unavailable",
                        headers={"Retry-After": "5"})

    @app.exception_handler(UpstreamUnavailable)
    async def _upstream(request: Request, exc: UpstreamUnavailable):
        log.error("upstream unavailable (request %s): %s",
                  getattr(request.state, "request_id", "-"), exc)
        return envelope(request, 503, "upstream.unavailable",
                        "an AI backend is temporarily unavailable", headers={"Retry-After": "10"})

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        log.exception("unhandled error (request %s)", getattr(request.state, "request_id", "-"))
        return envelope(request, 500, "internal", "internal server error")
