"""Operational platform: migrations, the startup gate, the admin CLI, request
context propagation, LLM metering, and the HTTP envelope/headers/CORS."""

from __future__ import annotations

import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from lessonforge import request_context
from lessonforge.admin.cli import main as admin
from lessonforge.config import DatabaseConfig, Settings
from lessonforge.db import migrate as mig
from lessonforge.knowledge.scope import AccessScope
from lessonforge.platform import Platform
from lessonforge.providers.base import LLMClient, LLMResult, LLMUsage
from lessonforge.providers.metering import MeteredLLM
from lessonforge.request_context import RequestContext
from tests.conftest import api_client, build_container, make_principal


# ── migrations ───────────────────────────────────────────────────────────────
def test_migration_files_are_ordered_without_gaps():
    found = mig.discover()
    assert [m.version for m in found] == list(range(1, len(found) + 1))
    assert all(len(m.checksum) == 64 for m in found)


def test_bad_migration_sets_are_rejected(tmp_path):
    (tmp_path / "0001_a.sql").write_text("SELECT 1;")
    (tmp_path / "0003_c.sql").write_text("SELECT 1;")
    with pytest.raises(mig.MigrationError, match="gaps"):
        mig.discover(tmp_path)
    (tmp_path / "0003_c.sql").unlink()
    (tmp_path / "2_bad.sql").write_text("")
    with pytest.raises(mig.MigrationError, match="filename"):
        mig.discover(tmp_path)


@pytest.fixture
def scratch_db():
    """An EMPTY throwaway database (the session one is already migrated)."""
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set")
    import psycopg
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    name = f"lf_mig_{uuid.uuid4().hex[:8]}"
    admin_conn = psycopg.connect(url, autocommit=True)
    admin_conn.execute(f'CREATE DATABASE "{name}"')
    yield make_conninfo(**{**conninfo_to_dict(url), "dbname": name})
    admin_conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    admin_conn.close()


def test_migrate_is_idempotent_atomic_and_detects_drift(scratch_db):
    import psycopg

    from lessonforge.db.pool import Database

    real = mig.discover()
    with psycopg.connect(scratch_db, autocommit=True) as conn:
        assert mig.status(conn, real).pending == real
        assert mig.migrate(conn, real, log=lambda *_: None) == [m.version for m in real]
        assert mig.migrate(conn, real, log=lambda *_: None) == []          # idempotent
        assert mig.status(conn, real).current
        broken = [*real, mig.Migration(len(real) + 1, "broken",
                                       "CREATE TABLE half_done (x int); SELECT nope;")]
        with pytest.raises(psycopg.Error):
            mig.migrate(conn, broken, log=lambda *_: None)
        exists = conn.execute("SELECT to_regclass('half_done') IS NOT NULL").fetchone()[0]
        assert exists is False                                               # all or nothing
        edited = [mig.Migration(1, real[0].name, real[0].sql + "\n-- edited"), *real[1:]]
        assert mig.status(conn, edited).drifted == [1]
        with pytest.raises(mig.MigrationError, match="immutable"):
            mig.migrate(conn, edited, log=lambda *_: None)

    db = Database(DatabaseConfig(provider="postgres", url=scratch_db))
    mig.assert_current(db)
    db.close()


def test_api_refuses_to_start_with_a_stale_schema(scratch_db, base_settings_dict):
    settings = Settings(**{**base_settings_dict, "database": {"provider": "postgres",
                                                              "url": scratch_db}})
    platform = Platform(settings)
    with pytest.raises(mig.SchemaOutOfDate):
        platform.validate(require_auth=False)
    platform.close()


# ── admin CLI ────────────────────────────────────────────────────────────────
@pytest.fixture(params=["memory", "postgres"])
def platform(request, base_settings_dict):
    if request.param == "memory":
        p = Platform(Settings(**base_settings_dict))
    else:
        db = request.getfixturevalue("pg")
        p = Platform(Settings(**{**base_settings_dict, "database": {"provider": "postgres",
                                                                    "url": db.cfg.url}}))
        p.__dict__["db"] = db                       # share the session pool
    p.close = lambda: None                          # keep state across CLI invocations
    return p


def test_cli_grant_revoke_and_audit(platform, capsys):
    assert admin(["grant", "--subject", "teacher:1"], platform=platform) == 0
    assert admin(["grant", "--subject", "teacher:1"], platform=platform) == 0   # idempotent
    rec = platform.directory.find("edboox", "teacher:1")
    assert "superadmin" in rec.grants and rec.external_role == "pending"   # before first login
    admin(["grants"], platform=platform)
    assert "edboox:teacher:1\tsuperadmin" in capsys.readouterr().out
    assert admin(["revoke", "--subject", "teacher:1"], platform=platform) == 0
    assert admin(["revoke", "--subject", "teacher:1"], platform=platform) == 1
    actions = [e.action for e in platform.audit.recent(10)]
    assert actions.count("grant.add") == 2 and "grant.remove" in actions
    assert all(e.actor.startswith("cli:") for e in platform.audit.recent(10))


def test_cli_disable_forget_purge(platform, capsys):
    admin(["grant", "--subject", "student:9"], platform=platform)
    assert admin(["disable", "--subject", "student:9"], platform=platform) == 0
    assert platform.directory.find("edboox", "student:9").disabled
    assert admin(["forget", "--subject", "student:9"], platform=platform) == 2    # needs --yes
    assert admin(["forget", "--subject", "student:9", "--yes"], platform=platform) == 0
    assert platform.directory.find("edboox", "student:9") is None
    assert admin(["purge"], platform=platform) == 0
    assert "purged" in capsys.readouterr().out


def test_cli_rejects_ungrantable_roles():
    with pytest.raises(SystemExit):
        admin(["grant", "--subject", "x", "--role", "teacher"])


# ── request context propagation ──────────────────────────────────────────────
def test_context_reaches_sync_routes_streams_and_workers(base_settings_dict):
    """The scope bound by the auth dependency must be what retrieval sees —
    inside sync routes (threadpool), SSE generators, and executor workers."""
    c = build_container(Settings(**base_settings_dict))
    seen: list[AccessScope] = []
    original = c.retriever.retrieve

    def spy(*a, **kw):
        seen.append(request_context.current_scope())
        return original(*a, **kw)
    c.retriever.retrieve = spy
    teacher = make_principal("teacher", tenant="sch-42")
    client = api_client(c, teacher)
    client.post("/api/v1/grounding/preview", json={"topic": "light"})          # sync route
    cid = client.post("/api/v1/chat/conversations", json={}).json()["id"]
    client.post(f"/api/v1/chat/conversations/{cid}/message", json={"message": "light?"})  # SSE
    assert seen and all(s.spaces == {"global", "school:sch-42"} for s in seen)

    ctx = RequestContext(principal=teacher, access_scope=AccessScope.of("school:sch-42"))
    with request_context.bound(ctx), ThreadPoolExecutor(1) as pool:
        inherited = request_context.submit_in_context(pool, request_context.current_scope)
        bare = pool.submit(request_context.current_scope)
        assert inherited.result().spaces == {"school:sch-42"}
        assert bare.result() == AccessScope.public()          # least privilege, never wider


def test_concurrent_requests_never_share_context(base_settings_dict):
    results: dict[str, AccessScope] = {}

    def worker(tenant: str) -> None:
        with request_context.bound(RequestContext(access_scope=AccessScope.of(f"school:{tenant}"))):
            threading.Event().wait(0.01)
            results[tenant] = request_context.current_scope()
    threads = [threading.Thread(target=worker, args=(f"s{i}",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(results[f"s{i}"].spaces == {f"school:s{i}"} for i in range(8))


class _CountingLLM(LLMClient):
    def complete(self, prompt, *, system=None, json_schema=None, temperature=None):
        return LLMResult(text="ok", usage=LLMUsage(tokens_in=7, tokens_out=3, model="m1"))

    def stream(self, prompt, *, system=None, temperature=None, on_usage=None):
        yield "a"
        if on_usage:
            on_usage(LLMUsage(tokens_in=11, tokens_out=2, model="m2"))

    def health(self):
        return True


def test_metered_llm_sums_usage_into_the_request():
    llm = MeteredLLM(_CountingLLM())
    llm.complete("x")                                  # no request bound → nothing recorded
    ctx = RequestContext()
    with request_context.bound(ctx):
        llm.complete("x")
        assert list(llm.stream("y")) == ["a"]
    assert ctx.usage.snapshot() == {"tokens_in": 18, "tokens_out": 5, "llm_calls": 2,
                                    "model": "m1,m2"}


# ── HTTP envelope, headers, CORS ─────────────────────────────────────────────
@pytest.fixture
def client(base_settings_dict):
    c = build_container(Settings(**base_settings_dict))
    return api_client(c, make_principal("student"))


def test_error_envelope_shapes(client):
    r404 = client.get("/api/v1/chat/conversations/00000000-0000-4000-8000-000000000000")
    assert r404.status_code == 404 and r404.json()["error"]["code"] == "not_found"
    r403 = client.get("/api/v1/documents")
    assert r403.json()["error"]["code"] == "perm.denied"
    r422 = client.post("/api/v1/chat/conversations/x/message", json={"message": ""})
    body = r422.json()["error"]
    assert r422.status_code == 422 and body["code"] == "validation" and body["details"][0]["loc"]
    r405 = client.put("/health")
    assert r405.json()["error"]["code"] == "method_not_allowed"
    bad_cursor = client.get("/api/v1/chat/conversations?cursor=%%%")
    assert bad_cursor.status_code == 422 and bad_cursor.json()["error"]["message"] == "invalid cursor"


def test_request_id_and_security_headers(client):
    r = client.get("/health", headers={"X-Request-ID": "trace-abc-123"})
    assert r.headers["x-request-id"] == "trace-abc-123"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "same-origin"
    assert "frame-ancestors" in r.headers["content-security-policy"]
    hostile = client.get("/health", headers={"X-Request-ID": "<script>"})
    assert hostile.headers["x-request-id"] != "<script>"


def test_cors_allows_only_the_configured_origin(base_settings_dict, monkeypatch):
    monkeypatch.setenv("EDBOOX_WEB_ORIGIN", "https://app.edboox.test")
    client = api_client(build_container(Settings(**base_settings_dict)), make_principal())
    ok = client.options("/api/v1/me", headers={"Origin": "https://app.edboox.test",
                                               "Access-Control-Request-Method": "GET"})
    assert ok.headers.get("access-control-allow-origin") == "https://app.edboox.test"
    assert ok.headers.get("access-control-allow-credentials") == "true"
    evil = client.options("/api/v1/me", headers={"Origin": "https://evil.test",
                                                 "Access-Control-Request-Method": "GET"})
    assert "access-control-allow-origin" not in evil.headers


def test_health_endpoints(client):
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/health/ready").json() == {"status": "ok", "database": True}


def test_llm_outage_is_503_not_500(base_settings_dict):
    from lessonforge.providers.base import UpstreamUnavailable

    class DownLLM(_CountingLLM):
        def complete(self, *a, **kw):
            raise UpstreamUnavailable("ollama down")
    c = build_container(Settings(**base_settings_dict), llm=DownLLM())
    r = api_client(c, make_principal("teacher")).post(
        "/api/v1/lessons/generate", json={"topic": "x", "grade": 6, "subject": "Science"})
    assert r.status_code == 503 and r.json()["error"]["code"] == "upstream.unavailable"
    assert r.headers["retry-after"] == "10"
