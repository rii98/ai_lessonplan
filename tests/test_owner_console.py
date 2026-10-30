"""Owner console: the AdminService (shared by HTTP and CLI) and its API — people
search, disable/enable, refresh from Edboox, delete-their-data, read-only grants,
audit paging, maintenance — plus the live sign-in demo endpoints (off by default)."""

from __future__ import annotations

from dataclasses import replace

import httpx
import pytest
import respx

from lessonforge.auth.directory import (
    MemoryPrincipalDirectory,
    PeopleFilter,
    PostgresPrincipalDirectory,
)
from lessonforge.auth.verifier import VerifiedToken
from lessonforge.config import Settings
from lessonforge.services.admin import Actor, AdminError
from tests.auth_helpers import JWKS_URL, auth_settings, jwks, mint
from tests.conftest import api_client, build_container, make_principal


def _vt(sub, role="student", tenant="sch-7", name=None):
    return VerifiedToken(issuer="edboox", subject=sub, role=role, tenant_id=tenant,
                         display_name=name or sub, grade=None, jti=None, expires_at=0, issued_at=0)


# ── directory search/stats contract (memory + postgres) ──────────────────────
@pytest.fixture(params=["memory", "postgres"])
def directory(request):
    if request.param == "memory":
        return MemoryPrincipalDirectory()
    return PostgresPrincipalDirectory(request.getfixturevalue("pg"))


def test_people_search_filters_pages_and_stats(directory):
    for i in range(5):
        directory.upsert(_vt(f"student:{i}", name=f"Student {i}"))
    directory.upsert(_vt("teacher:1", "teacher", name="Sita Sharma"))
    directory.upsert(_vt("teacher:9", "teacher", tenant="sch-9", name="Maya"))
    directory.grant("edboox", "teacher:1", "superadmin", granted_by="t")
    directory.set_disabled("edboox", "student:0", True)

    p1 = directory.search(PeopleFilter(), limit=4)
    p2 = directory.search(PeopleFilter(), limit=4, cursor=__import__(
        "lessonforge.pagination", fromlist=["Keyset"]).Keyset.decode(p1.next_cursor))
    assert len(p1.items) == 4 and len(p2.items) == 3
    assert {r.subject for r in p1.items} | {r.subject for r in p2.items} == {
        *(f"student:{i}" for i in range(5)), "teacher:1", "teacher:9"}
    assert [r.subject for r in directory.search(PeopleFilter(q="sita"), limit=10).items] == ["teacher:1"]
    assert [r.subject for r in directory.search(PeopleFilter(q="%"), limit=10).items] == []
    assert [r.subject for r in directory.search(PeopleFilter(role="superadmin"), limit=10).items] == ["teacher:1"]
    assert {r.subject for r in directory.search(PeopleFilter(role="teacher", tenant_id="sch-9"),
                                                limit=10).items} == {"teacher:9"}
    assert [r.subject for r in directory.search(PeopleFilter(disabled=True), limit=10).items] == ["student:0"]
    st = directory.stats()
    assert (st["total"], st["role:student"], st["role:teacher"], st["superadmins"], st["disabled"]) == (
        7, 5, 2, 1, 1)


# ── the console API ──────────────────────────────────────────────────────────
@pytest.fixture
def console(base_settings_dict):
    c = build_container(Settings(**base_settings_dict))
    d = c.platform.directory
    owner_rec = d.upsert(_vt("teacher:1", "teacher", name="Owner"))
    d.grant("edboox", "teacher:1", "superadmin", granted_by="cli:test")
    student = d.upsert(_vt("student:42", name="Aarav"))
    owner = replace(make_principal("teacher", subject="teacher:1", superadmin=True), id=owner_rec.id)
    return c, api_client(c, owner), owner, student


def test_overview_reports_health_schema_people_and_content(console):
    _, client, *_ = console
    d = client.get("/api/v1/admin/overview").json()
    assert d["health"]["database"] is True and d["schema"]["current"] is True
    assert d["people"]["superadmins"] == 1 and d["people"]["role:student"] == 1
    assert set(d["content"]) == {"documents", "chat"} and "reference" in d["corpus"]


def test_people_list_and_detail(console):
    _, client, _, student = console
    items = client.get("/api/v1/admin/people?role=student").json()["items"]
    assert [p["subject"] for p in items] == ["student:42"]
    detail = client.get(f"/api/v1/admin/people/{student.id}").json()
    assert detail["name"] == "Aarav" and detail["superadmin"] is False
    assert client.get("/api/v1/admin/people/00000000-0000-4000-8000-000000000000").status_code == 404


def test_disable_enable_takes_effect_and_is_audited(console):
    c, client, _, student = console
    assert client.post(f"/api/v1/admin/people/{student.id}/disable").json()["disabled"] is True
    assert c.platform.directory.get(student.id).disabled
    assert client.post(f"/api/v1/admin/people/{student.id}/enable").json()["disabled"] is False
    actions = [e["action"] for e in client.get("/api/v1/admin/audit").json()["items"]]
    assert actions[:2] == ["principal.enable", "principal.disable"]
    assert client.get("/api/v1/admin/audit").json()["items"][0]["actor"] == "edboox:teacher:1"


def test_guards_self_superadmins_and_typed_confirmation(console):
    _, client, owner, student = console
    me = client.post(f"/api/v1/admin/people/{owner.id}/disable")
    assert me.status_code == 409 and "your own" in me.json()["error"]["message"]
    wrong = client.post(f"/api/v1/admin/people/{student.id}/forget", json={"confirm": "oops"})
    assert wrong.status_code == 409 and "student:42" in wrong.json()["error"]["message"]
    ok = client.post(f"/api/v1/admin/people/{student.id}/forget", json={"confirm": "student:42"})
    assert ok.json() == {"deleted": True}
    assert client.get(f"/api/v1/admin/people/{student.id}").status_code == 404


def test_superadmins_cannot_be_removed_from_the_console(console):
    c, client, *_ = console
    other = c.platform.directory.upsert(_vt("teacher:5", "teacher"))
    c.platform.directory.grant("edboox", "teacher:5", "superadmin", granted_by="cli")
    r = client.post(f"/api/v1/admin/people/{other.id}/disable")
    assert r.status_code == 409 and "lessonforge-admin" in r.json()["error"]["message"]


def test_grants_are_read_only(console):
    _, client, *_ = console
    d = client.get("/api/v1/admin/grants").json()
    assert [g["subject"] for g in d["items"]] == ["teacher:1"] and "lessonforge-admin" in d["how_to_change"]
    assert client.post("/api/v1/admin/grants", json={}).status_code == 405   # no grant endpoint (D5)


def test_sync_context_maintenance_and_audit_paging(console):
    _, client, _, student = console
    out = client.post(f"/api/v1/admin/people/{student.id}/sync-context").json()
    assert out["refreshed"] is True and out["context"]["source"] == "claims"
    assert client.post("/api/v1/admin/maintenance/purge").json() == {"launch_tokens": 0,
                                                                     "activity_events": 0}
    counts = client.post("/api/v1/admin/maintenance/backfill-spaces").json()
    assert set(counts) >= {"reference", "pedagogical"}
    page = client.get("/api/v1/admin/audit?limit=2").json()
    older = client.get(f"/api/v1/admin/audit?limit=2&before={page['next_cursor']}").json()
    assert len(page["items"]) == 2 and older["items"]
    assert {e["id"] for e in page["items"]}.isdisjoint({e["id"] for e in older["items"]})
    only = client.get("/api/v1/admin/audit?action=maintenance.").json()["items"]
    assert only and all(e["action"].startswith("maintenance.") for e in only)


def test_console_page_and_nav_for_owners_only(console):
    c, client, *_ = console
    assert client.get("/admin").status_code == 200
    assert "/admin" in [n["path"] for n in client.get("/api/v1/me").json()["nav"]]
    teacher = api_client(c, make_principal("teacher"))
    assert "/admin" not in [n["path"] for n in teacher.get("/api/v1/me").json()["nav"]]
    assert teacher.get("/api/v1/admin/overview").status_code == 403


def test_admin_service_rejects_ungranted_actor():
    from lessonforge.platform import Platform
    from lessonforge.services.admin import AdminService

    p = Platform(Settings(llm={"provider": "ollama", "model": "m"},
                          embedding={"provider": "fastembed", "model": "e"},
                          reranker={"provider": "noop"}, vector_store={"provider": "qdrant"}))
    rec = p.directory.upsert(_vt("student:1"))
    with pytest.raises(AdminError, match="own account"):
        AdminService(p).set_disabled(rec.id, True, Actor("edboox:student:1", rec.id))


# ── live sign-in demo ────────────────────────────────────────────────────────
@pytest.fixture
def jwks_mock():
    with respx.mock(assert_all_called=False) as mock:
        mock.get(JWKS_URL).mock(return_value=httpx.Response(200, json=jwks()))
        yield


def test_demo_is_off_by_default(base_settings_dict, jwks_mock):
    client = api_client(build_container(auth_settings(base_settings_dict)), principal=None)
    assert client.get("/api/v1/demo/status").status_code == 404
    assert client.post("/api/v1/demo/launch", json={"token": mint()}).status_code == 404
    assert not any("demo" in p for p in client.app.openapi()["paths"])   # not in the contract


def test_demo_runs_the_real_launch_and_shows_attacks_refused(base_settings_dict, jwks_mock):
    c = build_container(auth_settings(base_settings_dict, demo={"enabled": True}))
    client = api_client(c, principal=None)
    assert client.get("/api/v1/demo/status").json()["issuers"][0]["ok"] is True
    token = mint("student:3", "student", school="sch-7", grade=8, ttl=60)
    r = client.post("/api/v1/demo/launch", json={"token": token})
    body = r.json()
    assert body["ok"] and [s["ok"] for s in body["steps"]] == [True] * 7
    assert body["lands_on"] == "/chat" and "lf_session" in r.cookies
    who = next(s for s in body["steps"] if s["title"].startswith("Who"))
    assert who["detail"]["permissions"] == ["chat"] and who["detail"]["new_here"] is True
    # the session is real: /me works with the cookie the demo set
    client.cookies.set("lf_session", r.cookies["lf_session"])
    assert client.get("/api/v1/me").json()["subject"] == "student:3"
    # replay is refused by the real replay guard
    again = client.post("/api/v1/demo/launch", json={"token": token}).json()
    assert again["ok"] is False and again["steps"][-1]["detail"]["code"] == "auth.invalid"
    assert "already used" in again["steps"][-1]["detail"]["message"]
    # tampering (exactly what the demo page does): edit the payload, keep the signature
    import base64
    import json as _json

    h, p, s = mint("student:4", "student").split(".")
    claims = _json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))
    claims.update(role="superadmin", school_id="sch-9")
    p2 = base64.urlsafe_b64encode(_json.dumps(claims).encode()).decode().rstrip("=")
    forged = client.post("/api/v1/demo/launch", json={"token": f"{h}.{p2}.{s}"}).json()
    assert forged["ok"] is False
    assert "Signature verification failed" in forged["steps"][-1]["detail"]["message"]
    ctx = client.post("/api/v1/demo/context", headers={"X-LF-Request": "1"}).json()
    assert ctx["provider"] == "claims" and ctx["after"]["student"]["grade"] == 8
