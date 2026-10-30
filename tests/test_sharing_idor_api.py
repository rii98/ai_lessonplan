"""IDOR and school sharing through the API (§11, §17)."""

from __future__ import annotations

import pytest

from lessonforge.config import Settings
from tests.conftest import FakeLLM, api_client, as_principal, build_container, make_principal


@pytest.fixture
def env(base_settings_dict, valid_ldd_dict):
    c = build_container(Settings(**base_settings_dict), llm=FakeLLM(response=valid_ldd_dict))
    people = {
        "sita": make_principal("teacher", tenant="sch-7", name="Sita Sharma"),
        "ram": make_principal("teacher", tenant="sch-7"),
        "maya": make_principal("teacher", tenant="sch-9"),
        "aarav": make_principal("student", tenant="sch-7", grade=8),
        "bikash": make_principal("student", tenant="sch-7", grade=8),
    }
    c.get_document_store().owner_names.update({p.id: p.display_name for p in people.values()})
    return api_client(c, people["sita"]), people


def _lesson(client) -> str:
    r = client.post("/api/v1/documents/lessons",
                    json={"topic": "Plants", "grade": 6, "subject": "Science", "duration_min": 45})
    assert r.status_code == 200, r.text
    return r.json()["document"]["id"]


def test_private_plan_is_404_to_colleagues_and_403_to_students(env):
    client, who = env
    doc = _lesson(client)
    as_principal(client, who["ram"])
    for method, url in (("get", f"/api/v1/documents/{doc}"),
                        ("get", f"/api/v1/documents/{doc}/versions"),
                        ("post", f"/api/v1/documents/{doc}/copy"),
                        ("post", f"/api/v1/documents/{doc}/undo"),
                        ("delete", f"/api/v1/documents/{doc}")):
        r = client.request(method.upper(), url)
        assert r.status_code == 404 and r.json()["error"]["code"] == "not_found", url
    as_principal(client, who["aarav"])
    assert client.get(f"/api/v1/documents/{doc}").status_code == 403   # no author permission


def test_school_sharing_lifecycle(env):
    client, who = env
    doc = _lesson(client)
    shared = client.patch(f"/api/v1/documents/{doc}", json={"visibility": "school"})
    assert shared.status_code == 200 and shared.json()["shared_at"]

    as_principal(client, who["ram"])                                   # same school
    listing = client.get("/api/v1/documents", params={"scope": "school"}).json()["items"]
    assert [d["id"] for d in listing] == [doc] and listing[0]["owner_name"] == "Sita Sharma"
    assert client.get(f"/api/v1/documents/{doc}").status_code == 200
    for method, url, body in (("patch", f"/api/v1/documents/{doc}", {"visibility": "private"}),
                              ("post", f"/api/v1/documents/{doc}/undo", None),
                              ("post", f"/api/v1/documents/{doc}/refine",
                               {"target": "materials", "instruction": "x"}),
                              ("delete", f"/api/v1/documents/{doc}", None)):
        r = client.request(method.upper(), url, json=body)
        assert r.status_code == 403 and r.json()["error"]["code"] == "perm.denied", url
    copy = client.post(f"/api/v1/documents/{doc}/copy", json={"title": "Ram's version"}).json()
    assert copy["document"]["copied_from"] == doc and copy["document"]["visibility"] == "private"
    mine = client.get("/api/v1/documents").json()["items"]
    assert [d["title"] for d in mine] == ["Ram's version"]

    as_principal(client, who["maya"])                                  # another school
    assert client.get(f"/api/v1/documents/{doc}").status_code == 404
    assert client.get("/api/v1/documents", params={"scope": "school"}).json()["items"] == []

    as_principal(client, who["sita"])
    client.patch(f"/api/v1/documents/{doc}", json={"visibility": "private"})
    as_principal(client, who["ram"])                                   # un-share is immediate
    assert client.get(f"/api/v1/documents/{doc}").status_code == 404
    assert client.get("/api/v1/documents", params={"scope": "school"}).json()["items"] == []


def test_students_cannot_reach_each_others_chats_or_quizzes(env):
    client, who = env
    as_principal(client, who["aarav"])
    cid = client.post("/api/v1/chat/conversations", json={"title": "mine"}).json()["id"]
    as_principal(client, who["bikash"])
    for method, url in (("get", f"/api/v1/chat/conversations/{cid}"),
                        ("patch", f"/api/v1/chat/conversations/{cid}"),
                        ("delete", f"/api/v1/chat/conversations/{cid}")):
        r = client.request(method.upper(), url, json={} if method == "patch" else None)
        assert r.status_code == 404, url
    r = client.post(f"/api/v1/chat/conversations/{cid}/message", json={"message": "hi"})
    assert r.status_code == 404
    assert all(c["id"] != cid
               for c in client.get("/api/v1/chat/conversations").json()["items"])
    for url in ("/api/v1/chat/artifacts/00000000-0000-4000-8000-000000000000",
                "/api/v1/chat/attempts/00000000-0000-4000-8000-000000000000", ):
        assert client.get(url).status_code == 404


def test_malformed_ids_are_404_not_500(env):
    client, _ = env
    for url in ("/api/v1/documents/not-a-uuid", "/api/v1/units/%20",
                "/api/v1/chat/conversations/zzz"):
        r = client.get(url)
        assert r.status_code == 404, (url, r.text)


def test_profile_is_per_principal(env):
    client, who = env
    client.put("/api/v1/profile", json={"style_notes": "sita's voice"})
    as_principal(client, who["ram"])
    assert client.get("/api/v1/profile").json()["style_notes"] == ""
