"""The web client is served as a static SPA: the shell for every client route
(never cached), hashed assets cached forever, nothing shadowing the API."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from lessonforge.api import webui
from lessonforge.api.main import create_app

CLIENT_ROUTES = ["/", "/lessons/doc-1", "/units", "/units/new", "/units/u1/days/2", "/studio/quiz",
                 "/library", "/settings", "/chat", "/chat/c-9", "/corpus", "/admin", "/admin/people/p1"]


@pytest.fixture
def raw() -> TestClient:
    # no auth override: pages must be public (the client sends people to sign in)
    return TestClient(create_app())


@pytest.mark.parametrize("path", CLIENT_ROUTES)
def test_every_client_route_serves_the_shell_uncached(raw, path):
    r = raw.get(path)
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert 'id="root"' in r.text and "/assets/" in r.text
    assert r.headers["cache-control"] == "no-cache"


def test_hashed_assets_are_immutable(raw):
    r = raw.get("/assets/app-abc123.js")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert raw.get("/assets/nope.js").status_code == 404


def test_unknown_paths_and_the_api_are_not_shadowed(raw):
    assert raw.get("/no-such-page").status_code == 404
    assert raw.get("/health").json() == {"status": "ok"}
    assert raw.get("/openapi.json").headers["content-type"].startswith("application/json")
    assert raw.get("/api/v1/nope").headers["content-type"].startswith("application/json")  # API 404, not the shell
    assert raw.get("/static/lf.js").status_code == 404        # the legacy pages are gone


def test_a_missing_build_is_a_clear_503(raw, monkeypatch, tmp_path):
    monkeypatch.setenv("LF_WEB_DIST", str(tmp_path / "empty"))
    r = raw.get("/library")
    assert r.status_code == 503 and "npm --prefix web" in r.text
    assert r.headers["cache-control"] == "no-store"


def test_spa_routes_mirror_the_server_nav():
    # every destination /me can hand out must be a client route
    from lessonforge.api.routers.platform import _PAGES

    assert {p for p, _, _ in _PAGES} <= set(webui.SPA_ROUTES)


def test_head_works_for_monitors_and_link_previews(raw):
    r = raw.head("/library")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-cache"
