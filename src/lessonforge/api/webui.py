"""Serving the web client (``web/`` — React + TypeScript, built by Vite).

The UI is a static single-page app: it embeds no data and calls only
``/api/v1``, so the API stays the whole contract and Edboox can host the same
build (or its own UI) anywhere (D3). This module only knows how to hand out the
built files:

- ``/assets/*`` — content-hashed bundles, cached for a year (``immutable``);
- every client route (``SPA_ROUTES``) — the app shell ``index.html``, never
  cached, so a deploy is picked up on the next navigation.

Where the build lives: ``$LF_WEB_DIST`` (Docker builds it in a Node stage), else
the copy packaged next to this file (``api/webui/``, written by
``npm --prefix web run build``). A missing build is a clear 503, not a blank page.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, HTMLResponse, Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

_PACKAGED = Path(__file__).resolve().parent / "webui"

# Client routes (web/src/app/routes.tsx). Explicit, so unknown paths stay 404 and
# nothing can shadow /api, /docs or /health.
SPA_ROUTES: tuple[str, ...] = (
    "/", "/lessons/{rest:path}", "/units", "/units/{rest:path}", "/studio/{rest:path}",
    "/library", "/settings", "/chat", "/chat/{rest:path}", "/corpus", "/admin", "/admin/{rest:path}",
)

_MISSING = """<!doctype html><meta charset="utf-8"><title>LessonForge</title>
<body style="font:16px system-ui;max-width:40rem;margin:4rem auto;padding:0 1rem">
<h1>The web app isn't built</h1>
<p>Run <code>npm --prefix web ci &amp;&amp; npm --prefix web run build</code>, or set
<code>LF_WEB_DIST</code> to a built copy. The API is up — see <a href="/docs">/docs</a>.</p>"""


def web_dist() -> Path:
    """The directory holding the built client (``index.html`` + ``assets/``)."""
    env = os.environ.get("LF_WEB_DIST", "").strip()
    return Path(env) if env else _PACKAGED


class ImmutableAssets(StaticFiles):
    """Hashed bundles never change under a name — let browsers keep them."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        resp = await super().get_response(path, scope)
        if resp.status_code == 200:
            resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return resp


def app_shell() -> Response:
    index = web_dist() / "index.html"
    if not index.is_file():
        return HTMLResponse(_MISSING, status_code=503, headers={"Cache-Control": "no-store"})
    return FileResponse(index, media_type="text/html", headers={"Cache-Control": "no-cache"})


router = APIRouter(include_in_schema=False)
for _path in SPA_ROUTES:
    router.add_api_route(_path, app_shell, methods=["GET", "HEAD"], response_class=HTMLResponse)


def assets_app() -> ImmutableAssets:
    """Mounted at ``/assets``; ``check_dir=False`` so the API boots without a build."""
    return ImmutableAssets(directory=web_dist() / "assets", check_dir=False)
