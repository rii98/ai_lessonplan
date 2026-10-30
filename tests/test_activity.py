"""Activity tracking (§15): the privacy guarantee of the event catalogue, feed and
report semantics (memory AND Postgres), retention, anonymisation on delete, the
buffered writer, tenant scoping of the API, and emission from real flows."""

from __future__ import annotations

import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from lessonforge import request_context
from lessonforge.activity import events as act
from lessonforge.activity.store import (
    BufferedRecorder,
    MemoryActivityStore,
    PostgresActivityStore,
    ReportFilter,
)
from lessonforge.auth.principal import Perm
from lessonforge.config import Settings
from lessonforge.request_context import RequestContext
from tests.conftest import FakeLLM, api_client, build_container, make_principal, pg_principal

NOW = datetime.now(UTC)


# ── the catalogue is the privacy guarantee ───────────────────────────────────
@pytest.mark.parametrize("type_", sorted(act.CATALOGUE))
def test_every_event_type_rejects_free_text(type_):
    model, _ = act.CATALOGUE[type_]
    assert model.model_config.get("extra") == "forbid"
    for field_name, field in model.model_fields.items():
        if field.annotation in (str, str | None):   # every string must be bounded/shaped
            meta = [m for m in field.metadata if hasattr(m, "max_length")]
            assert meta and meta[0].max_length, f"{type_}.{field_name} is unbounded free text"
    with pytest.raises(ValidationError):
        model.model_validate({"message": "my secret question"})


def test_identifier_fields_refuse_sentences():
    with pytest.raises(ValidationError):
        act.ChatMessage(intent="please make me a quiz about my life")


def test_build_event_takes_the_actor_from_the_request_and_needs_one():
    assert act.build_event("session.started", act.SessionStarted(channel="api")) is None
    p = make_principal("student", tenant="sch-7", grade=8, subject="student:42")
    with request_context.bound(RequestContext(principal=p)):
        e = act.build_event("quiz.attempt.started", act.QuizAttemptStarted(quiz_id="q1"),
                            resource_type="quiz", resource_id="q1")
    assert (e.subject, e.role, e.tenant_id, e.grade) == ("student:42", "student", "sch-7", 8)
    with pytest.raises(TypeError):
        act.build_event("chat.message", act.SessionStarted(channel="api"))


# ── store contract (memory + postgres) ───────────────────────────────────────
@pytest.fixture(params=["memory", "postgres"])
def env(request):
    if request.param == "memory":
        return MemoryActivityStore(), lambda **kw: make_principal(**kw)
    db = request.getfixturevalue("pg")
    return PostgresActivityStore(db), lambda **kw: pg_principal(db, **kw)


def _ev(p, type_, props, *, at=None, resource_id=None, grade=None):
    return act.ActivityEvent(type=type_, props=props.model_dump(mode="json", exclude_none=True),
                             role=p.role, principal_id=p.id, subject=p.subject,
                             tenant_id=p.tenant_id, grade=grade, resource_type="quiz",
                             resource_id=resource_id, occurred_at=at or NOW)


def _populate(store, person):
    s7a = person(role="student", tenant="sch-7")
    s7b = person(role="student", tenant="sch-7")
    s9 = person(role="student", tenant="sch-9")
    t7 = person(role="teacher", tenant="sch-7")
    q1, q2 = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"
    msg = lambda **kw: act.ChatMessage(intent="qa", subject="Science", tokens_in=10,
                                       tokens_out=5, **kw)
    store.record_many([
        _ev(s7a, "chat.message", msg(chapters=["Light"], grounded=True), grade=8),
        _ev(s7a, "chat.message", msg(chapters=["Light", "Sound"], grounded=False), grade=8),
        _ev(s7b, "chat.message", msg(chapters=["Sound"], grounded=True), grade=8),
        _ev(s9, "chat.message", msg(chapters=["Force"], grounded=True), grade=10),
        _ev(t7, "lesson.generated", act.Generated(grade=8, subject="Science", tokens_in=100,
                                                  tokens_out=50)),
        _ev(t7, "quiz.created", act.QuizCreated(source="chat", questions=5, chapter="Light"),
            resource_id=q1),
        _ev(t7, "quiz.created", act.QuizCreated(source="chat", questions=5, chapter="Sound"),
            resource_id=q2),
        _ev(s7a, "quiz.attempt.started", act.QuizAttemptStarted(quiz_id=q1), resource_id=q1),
        _ev(s7a, "quiz.attempt.finished", act.QuizAttemptFinished(score=4, total=5, percent=80,
                                                                  duration_s=60), resource_id=q1),
        _ev(s7b, "quiz.attempt.started", act.QuizAttemptStarted(quiz_id=q2), resource_id=q2),
        _ev(s7b, "quiz.attempt.finished", act.QuizAttemptFinished(score=1, total=5, percent=20,
                                                                  duration_s=30), resource_id=q2),
        _ev(s7b, "quiz.attempt.started", act.QuizAttemptStarted(quiz_id=q2), resource_id=q2),
        _ev(s7a, "chat.message", msg(), at=NOW - timedelta(days=500)),        # out of range
    ])
    return s7a, s9


def _window(**kw):
    return ReportFilter(start=NOW - timedelta(days=1), end=NOW + timedelta(minutes=1), **kw)


def test_feed_is_gap_free_replayable_and_tenant_scoped(env):
    store, person = env
    _populate(store, person)
    seen, cursor = [], 0
    while True:
        page = store.feed(after=cursor, limit=4, types=None, tenant_id=None)
        seen += [e.id for e in page.items]
        if page.next_cursor is None:
            break
        cursor = int(page.next_cursor)
    assert seen == sorted(seen) and len(seen) == 13
    replay = store.feed(after=seen[4], limit=100, types=None, tenant_id=None).items
    assert [e.id for e in replay] == seen[5:]                     # resume exactly where stopped
    only9 = store.feed(after=0, limit=100, types=None, tenant_id="sch-9").items
    assert {e.event.tenant_id for e in only9} == {"sch-9"}
    typed = store.feed(after=0, limit=100, types=["quiz.created"], tenant_id=None).items
    assert {e.event.type for e in typed} == {"quiz.created"}


def test_usage_report(env):
    store, person = env
    _populate(store, person)
    rows = store.usage(_window(), ["school", "role"])
    by = {(r["school"], r["role"]): r for r in rows}
    assert by[("sch-7", "student")]["chat_messages"] == 3
    assert by[("sch-7", "student")]["active_users"] == 2
    assert by[("sch-7", "teacher")]["lessons"] == 1 and by[("sch-7", "teacher")]["tokens_in"] == 100
    assert by[("sch-9", "student")]["chat_messages"] == 1
    total = store.usage(_window(), [])[0]
    assert total["chat_messages"] == 4 and total["quiz_attempts"] == 2
    assert store.usage(_window(tenant_id="sch-9"), [])[0]["chat_messages"] == 1
    days = store.usage(_window(), ["day"])
    assert len(days) == 1 and str(days[0]["day"]) == NOW.date().isoformat()


def test_chat_topics_report(env):
    store, person = env
    _populate(store, person)
    r = store.chat_topics(_window(tenant_id="sch-7"))
    assert r["turns"] == 3 and r["grounded_share"] == round(2 / 3, 4)
    assert r["subjects"] == [{"subject": "Science", "turns": 3}]
    assert [(c["chapter"], c["turns"]) for c in r["chapters"]] == [("Light", 2), ("Sound", 2)]


def test_quiz_report(env):
    store, person = env
    _populate(store, person)
    r = store.quizzes(_window(tenant_id="sch-7"))
    assert (r["created"], r["attempts_started"], r["attempts_finished"]) == (2, 3, 2)
    assert r["completion_rate"] == round(2 / 3, 4) and r["avg_percent"] == 50.0
    assert [w["chapter"] for w in r["weakest_chapters"]] == ["Sound", "Light"]


def test_user_summary_and_retention(env):
    store, person = env
    s7a, _ = _populate(store, person)
    u = store.user_summary(s7a.subject, _window())
    assert u["events"]["chat.message"] == 2 and u["avg_quiz_percent"] == 80.0
    assert store.purge_older_than(400) == 1
    assert store.user_summary(s7a.subject, ReportFilter(
        start=NOW - timedelta(days=900), end=NOW + timedelta(days=1)))["events"]["chat.message"] == 2


def test_deleting_a_principal_anonymises_their_events(pg):
    from lessonforge.auth.directory import PostgresPrincipalDirectory

    store = PostgresActivityStore(pg)
    p = pg_principal(pg, "student", subject="student:777")
    store.record_many([_ev(p, "session.started", act.SessionStarted(channel="api"))])
    assert PostgresPrincipalDirectory(pg).delete("edboox", "student:777")
    e = store.feed(after=0, limit=10, types=None, tenant_id=None).items[0].event
    assert e.principal_id is None and e.subject is None and e.tenant_id == "sch-1"  # counts survive


def test_buffered_recorder_batches_and_never_raises():
    sink = MemoryActivityStore()
    rec = BufferedRecorder(sink, batch=50, flush_interval_s=0.05)
    p = make_principal()
    for _ in range(120):
        rec.record(_ev(p, "session.started", act.SessionStarted(channel="api")))
    rec.close()
    assert len(sink.rows) == 120

    class Broken(MemoryActivityStore):
        def record_many(self, events):
            raise RuntimeError("db down")
    broken = BufferedRecorder(Broken(), flush_interval_s=0.01)
    broken.record(_ev(p, "session.started", act.SessionStarted(channel="api")))   # no raise
    time.sleep(0.05)
    broken.close()


# ── API: tenant scoping + emission from real flows ───────────────────────────
@pytest.fixture
def app_env(base_settings_dict, valid_ldd_dict):
    c = build_container(Settings(**base_settings_dict), llm=FakeLLM(response=valid_ldd_dict))
    return c


def test_reports_api_is_tenant_scoped(app_env):
    c = app_env
    _populate(c.platform.activity_reader, lambda **kw: make_principal(**kw))
    service = make_principal("service", tenant=None, subject="service:edboox")
    client = api_client(c, service)
    assert len(client.get("/api/v1/events?limit=1000").json()["items"]) == 13
    assert client.get("/api/v1/reports/usage?group_by=school").json()["rows"]
    school_admin = replace(make_principal("teacher", tenant="sch-9"),
                           permissions=frozenset({Perm.REPORTS_READ}))
    client = api_client(c, school_admin)
    items = client.get("/api/v1/events").json()["items"]
    assert items and {e["tenant_id"] for e in items} == {"sch-9"}
    assert client.get("/api/v1/events?school=sch-7").status_code == 403
    assert client.get("/api/v1/reports/chat-topics").json()["turns"] == 1
    assert client.get("/api/v1/events?types=bogus").status_code == 422
    assert client.get("/api/v1/reports/usage?group_by=planet").status_code == 422
    no_school = replace(school_admin, tenant_id=None)
    assert api_client(c, no_school).get("/api/v1/events").status_code == 403


def test_real_flows_emit_content_free_events(app_env):
    c = app_env
    teacher = make_principal("teacher", tenant="sch-7")
    client = api_client(c, teacher)
    assert client.post("/api/v1/lessons/generate", json={
        "topic": "Plants", "grade": 6, "subject": "Science", "duration_min": 45}).status_code == 200
    cid = client.post("/api/v1/chat/conversations", json={}).json()["id"]
    client.post(f"/api/v1/chat/conversations/{cid}/message",
                json={"message": "MY PRIVATE QUESTION about leaves"})
    events = c.platform.activity_reader.feed(after=0, limit=100, types=None, tenant_id=None).items
    types = [e.event.type for e in events]
    assert {"lesson.generated", "chat.conversation.created", "chat.message"} <= set(types)
    assert all(e.event.subject == teacher.subject for e in events)
    blob = repr([e.event for e in events])
    assert "MY PRIVATE QUESTION" not in blob and "leaves" not in blob   # never message text



def test_subjects_in_events_are_canonical_or_dropped():
    assert act.ChatMessage(intent="qa", subject="science and technology").subject == "Science"
    assert act.ChatMessage(intent="qa", subject="my crush's name").subject is None
    assert act.ConversationCreated(scope_mode="off", subject="whatever I typed").subject is None
