"""Auth through the real app (no dependency override): the launch flow, both
carriers, CSRF, the error envelope, /me — and the ROLE MATRIX: every /api/v1
operation × every role × {bearer, cookie}, generated from the live OpenAPI table
so a new route can't slip through unclassified (§11)."""

from __future__ import annotations

import re
import uuid

import httpx
import pytest
import respx

from lessonforge.auth.principal import Perm
from tests.auth_helpers import JWKS_URL, LOGIN_URL, auth_settings, jwks, mint
from tests.conftest import api_client, build_container

# Which permission each API area requires. EVERY /api/v1 path must match one row
# (test_every_route_is_classified) — that is what keeps the matrix from drifting.
AREAS: list[tuple[str, Perm | None]] = [
    (r"^/api/v1/me$", None),                               # authentication only
    (r"^/api/v1/me/", Perm.CHAT),                          # the learner's memory & progress
    (r"^/api/v1/chat/", Perm.CHAT),
    (r"^/api/v1/(lessons|documents|units|artifacts|export|profile|grounding)(/|$)", Perm.AUTHOR),
    (r"^/api/v1/corpus/", Perm.CORPUS_MANAGE),
    (r"^/api/v1/health/providers$", Perm.OPS),
    (r"^/api/v1/(events|reports/)", Perm.REPORTS_READ),
    (r"^/api/v1/admin/", Perm.ADMIN),
]
ROLES = {  # role → (sub, token role claim, school, granted superadmin?)
    "student": ("student:1", "student", "sch-7", False),
    "teacher": ("teacher:2", "teacher", "sch-7", False),
    "service": ("service:edboox", "service", None, False),
    "superadmin": ("teacher:1", "teacher", "sch-7", True),
}
PERMS = {"student": {Perm.CHAT}, "teacher": {Perm.CHAT, Perm.AUTHOR},
         "service": {Perm.REPORTS_READ}, "superadmin": set(Perm)}
_PARAMS = {"kind": "quiz", "name": "reference", "day": "1", "collection": "reference",
           "doc_id": "x", "subject": "student:1"}


def _required(path: str) -> Perm | None:
    for pattern, perm in AREAS:
        if re.search(pattern, path):
            return perm
    raise AssertionError(f"unclassified API path {path}")


@pytest.fixture
def jwks_mock():
    with respx.mock(assert_all_called=False) as mock:
        mock.get(JWKS_URL).mock(return_value=httpx.Response(200, json=jwks()))
        yield mock


@pytest.fixture
def env(base_settings_dict, jwks_mock):
    container = build_container(auth_settings(base_settings_dict))
    container.platform.directory.grant("edboox", "teacher:1", "superadmin", granted_by="test")
    return container, api_client(container, principal=None, follow_redirects=False)


def _bearer(role: str) -> dict[str, str]:
    sub, claim, school, _ = ROLES[role]
    return {"Authorization": f"Bearer {mint(sub, claim, school=school)}"}


def _launch(client, role: str) -> str:
    sub, claim, school, _ = ROLES[role]
    r = client.post("/auth/launch", data={"token": mint(sub, claim, school=school, ttl=60)})
    assert r.status_code == 303, r.text
    return r.cookies["lf_session"]


# ── launch / session / csrf ──────────────────────────────────────────────────
def test_launch_sets_a_hardened_session_and_lands_by_role(env):
    _, client = env
    sub, claim, school, _ = ROLES["student"]
    r = client.post("/auth/launch", data={"token": mint(sub, claim, school=school, ttl=60)})
    assert r.status_code == 303 and r.headers["location"] == "/chat"   # students: chat only
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie
    sub, claim, school, _ = ROLES["teacher"]
    r = client.post("/auth/launch", data={"token": mint(sub, claim, school=school, ttl=60)})
    assert r.headers["location"] == "/"


def test_revisiting_the_launch_url_continues_into_the_app(env):
    _, client = env
    r = client.get("/auth/launch")
    assert r.status_code == 303 and r.headers["location"] == "/"


def test_launch_token_cannot_be_replayed_or_aged(env):
    _, client = env
    token = mint("teacher:2", "teacher", ttl=60)
    assert client.post("/auth/launch", data={"token": token}).status_code == 303
    again = client.post("/auth/launch", data={"token": token})
    assert again.status_code == 401 and again.json()["error"]["code"] == "auth.invalid"
    old = client.post("/auth/launch", data={"token": mint("teacher:2", "teacher", iat_offset=-600,
                                                          ttl=3600)})
    assert old.status_code == 401 and old.json()["error"]["code"] == "auth.expired"
    assert client.post("/auth/launch", data={}).status_code == 401


def test_session_cookie_authenticates_and_csrf_guards_writes(env):
    _, client = env
    session = _launch(client, "teacher")
    client.cookies.set("lf_session", session)
    assert client.get("/api/v1/me").status_code == 200                          # GET: no header needed
    no_csrf = client.post("/api/v1/chat/conversations", json={})
    assert no_csrf.status_code == 403 and "CSRF" in no_csrf.json()["error"]["message"]
    ok = client.post("/api/v1/chat/conversations", json={}, headers={"X-LF-Request": "1"})
    assert ok.status_code == 200


def test_missing_and_bad_credentials_use_the_envelope(env):
    _, client = env
    r = client.get("/api/v1/me")
    assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"
    body = r.json()["error"]
    assert body["code"] == "auth.missing" and body["details"]["login_url"] == LOGIN_URL
    assert body["request_id"] == r.headers["x-request-id"]
    bad = client.get("/api/v1/me", headers={"Authorization": "Bearer nonsense"})
    assert bad.status_code == 401 and bad.json()["error"]["code"] == "auth.invalid"
    expired = client.get("/api/v1/me", headers={"Authorization": f"Bearer {mint(ttl=-60)}"})
    assert expired.json()["error"]["code"] == "auth.expired"


def test_me_describes_the_caller_for_any_ui(env):
    _, client = env
    me = client.get("/api/v1/me", headers=_bearer("student")).json()
    assert me["role"] == "student" and me["permissions"] == ["chat"]
    assert [n["path"] for n in me["nav"]] == ["/chat"]
    assert me["knowledge_spaces"] == ["global", "school:sch-7"]
    admin = client.get("/api/v1/me", headers=_bearer("superadmin")).json()
    assert admin["role"] == "superadmin" and admin["knowledge_spaces"] == "*"


def test_logout_clears_the_session(env):
    _, client = env
    r = client.post("/auth/logout")
    assert r.status_code == 303 and r.headers["location"] == LOGIN_URL
    assert "lf_session=" in r.headers["set-cookie"]


def test_pages_are_static_shells_and_public(env):
    _, client = env
    for page in ("/", "/chat", "/units", "/library", "/corpus", "/admin"):
        r = client.get(page)
        assert r.status_code == 200 and 'id="root"' in r.text


# ── the role matrix ──────────────────────────────────────────────────────────
def _operations(client) -> list[tuple[str, str]]:
    spec = client.app.openapi()
    return sorted((m.upper(), p) for p, ops in spec["paths"].items() if p.startswith("/api/v1")
                  for m in ops)


def test_every_route_is_classified(env):
    _, client = env
    ops = _operations(client)
    assert len(ops) > 50
    for _, path in ops:
        _required(path)


@pytest.mark.parametrize("carrier", ["bearer", "cookie"])
@pytest.mark.parametrize("role", list(ROLES))
def test_role_matrix(env, role, carrier):
    _, client = env
    headers: dict[str, str] = {}
    if carrier == "bearer":
        headers = _bearer(role)
    else:
        client.cookies.set("lf_session", _launch(client, role))
        headers = {"X-LF-Request": "1"}
    for method, path in _operations(client):
        url = re.sub(r"\{(\w+)(?::path)?\}", lambda m: _PARAMS.get(m.group(1), str(uuid.uuid4())), path)
        kwargs = {"json": {}} if method in ("POST", "PUT", "PATCH") else {}
        r = client.request(method, url, headers=headers, **kwargs)
        need = _required(path)
        allowed = need is None or need in PERMS[role]
        assert r.status_code != 500, f"{role}/{carrier} {method} {path}: {r.text[:300]}"
        if allowed:
            assert r.status_code not in (401, 403), f"{role} denied {method} {path}: {r.text[:200]}"
        else:
            assert r.status_code == 403, f"{role} reached {method} {path}: {r.status_code}"
            assert r.json()["error"]["code"] == "perm.denied"
