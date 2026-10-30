"""User context (§6): vocabulary, the Edboox anti-corruption mapper, the HTTP
adapter, the cached fail-soft ContextService, and ScopePolicy (default / locked /
off) — plus the rule that context personalises but never authorizes."""

from __future__ import annotations

from concurrent.futures import Future
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from lessonforge.auth.directory import MemoryPrincipalDirectory
from lessonforge.auth.verifier import VerifiedToken
from lessonforge.config import ContextConfig, ScopeConfig, ScopeRoleConfig, Settings
from lessonforge.context.model import (
    StudentContext,
    TeacherContext,
    TeachingAssignment,
    UserContext,
)
from lessonforge.context.provider import ClaimsContextProvider, build_context_provider
from lessonforge.context.scope_policy import ScopePolicy, TurnScope
from lessonforge.context.service import ContextService
from lessonforge.domain.chat import ChatDefaults, FilterMode
from lessonforge.domain.profile import TeacherProfile
from lessonforge.domain.vocabulary import UnknownTermError, Vocabulary, load_vocabulary
from lessonforge.integrations.edboox import EdbooxHttpContextProvider, EdbooxMapper
from tests.conftest import FakeLLM, api_client, build_container, make_principal

EDBOOX = "https://edboox.test"


class SyncExecutor:
    def submit(self, fn, *args):
        f: Future = Future()
        f.set_result(fn(*args))
        return f


@pytest.fixture(scope="module")
def vocab() -> Vocabulary:
    return load_vocabulary()


# ── vocabulary ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize("raw,grade", [("Class 8", 8), ("grade 10", 10), ("VIII", 8),
                                       ("कक्षा ८", 8), (8, 8), ("12th", 12), ("13", None),
                                       ("", None), (None, None), (True, None)])
def test_grades(vocab, raw, grade):
    assert vocab.grade(raw) == grade


@pytest.mark.parametrize("raw,subject", [("Science and Technology", "Science"),
                                         ("  science  ", "Science"), ("विज्ञान", "Science"),
                                         ("Maths", "Mathematics"), ("Computer", "Computer Science"),
                                         ("Astrology", None)])
def test_subjects(vocab, raw, subject):
    assert vocab.subject(raw) == subject


def test_ingestion_rejects_unfilterable_subjects(vocab):
    assert vocab.normalize_metadata({"subject": "sci", "grade": "Class 6"}) == {
        "subject": "Science", "grade": 6}
    with pytest.raises(UnknownTermError):
        vocab.normalize_metadata({"subject": "Astrology"})


def test_conflicting_aliases_fail_loudly():
    with pytest.raises(ValueError, match="maps to both"):
        Vocabulary({"Science": ["sci"], "Social": ["sci"]}, [])


# ── mapper (Edboox shapes in, our vocabulary out) ────────────────────────────
STUDENT = {"sub": "student:42", "role": "student",
           "school": {"id": "sch-7", "name": "Shree Janata", "district": "Kaski",
                      "province": "Gandaki"},
           "student": {"grade": "Class 8", "section": "A", "medium": "nepali", "stream": None,
                       "optional_subjects": ["Computer Science", "Astrology"]},
           "teacher": None, "preferred_language": "ne"}
TEACHER = {"sub": "teacher:2", "role": "teacher", "school": {"id": "sch-7"},
           "teacher": {"assignments": [
               {"grade": "Class 8", "section": "A", "subject": "Science and Technology"},
               {"grade": "Class 99", "subject": "Science"},          # unmappable → dropped
               {"grade": "Class 10", "subject": "Compulsory Mathematics"}]},
           "preferred_language": "english"}


def test_mapper_student(vocab):
    ctx = EdbooxMapper(vocab).map(STUDENT)
    assert ctx.student.grade == 8 and ctx.student.medium == "ne" and ctx.language == "ne"
    assert ctx.student.optional_subjects == ["Computer Science"]
    assert ctx.school.district == "Kaski" and ctx.source == "edboox_http"
    assert ctx.prompt_facts() == {"grade": 8, "language": "ne", "district": "Kaski"}  # no ids/names


def test_mapper_teacher_and_missing_fields(vocab):
    ctx = EdbooxMapper(vocab).map(TEACHER)
    assert [(a.grade, a.subject) for a in ctx.teacher.assignments] == [
        (8, "Science"), (10, "Mathematics")]
    assert ctx.language == "en"
    assert EdbooxMapper(vocab).map({}).model_dump(exclude={"source"}) == UserContext().model_dump(
        exclude={"source"})


def test_http_provider_contract(vocab):
    cfg = ContextConfig(provider="edboox_http", base_url=EDBOOX, api_key="k3y")
    p = make_principal("student", subject="student:42")
    with respx.mock() as mock:
        route = mock.get(f"{EDBOOX}/api/v1/ai/users/student%3A42").mock(
            return_value=httpx.Response(200, json=STUDENT))
        ctx = EdbooxHttpContextProvider(cfg, vocab).fetch(p)
        assert route.calls[0].request.headers["X-Api-Key"] == "k3y"
    assert ctx.student.grade == 8
    with respx.mock() as mock:
        mock.get(url__startswith=EDBOOX).mock(return_value=httpx.Response(404))
        assert EdbooxHttpContextProvider(cfg, vocab).fetch(p) is None
    assert isinstance(build_context_provider(cfg, vocab), EdbooxHttpContextProvider)
    with pytest.raises(ValueError):
        build_context_provider(ContextConfig(provider="ldap"), vocab)


# ── service: cached, refreshed out of band, fails soft ───────────────────────
def _service(provider, directory=None, ttl_h=24.0):
    directory = directory or MemoryPrincipalDirectory()
    return ContextService(provider, directory, ttl_h=ttl_h, cache_s=0,
                          executor=SyncExecutor()), directory


def _registered(directory, role="student", grade=8):
    rec = directory.upsert(VerifiedToken(issuer="edboox", subject=f"{role}:9", role=role,
                                         tenant_id="sch-7", display_name="X", grade=grade,
                                         jti=None, expires_at=0, issued_at=0))
    p = make_principal(role, tenant="sch-7", subject=f"{role}:9", grade=grade)
    from dataclasses import replace
    return replace(p, id=rec.id)


def test_refresh_stores_context_and_reads_it_back(vocab):
    svc, directory = _service(EdbooxHttpContextProvider(
        ContextConfig(provider="edboox_http", base_url=EDBOOX), vocab))
    p = _registered(directory)
    with respx.mock() as mock:
        mock.get(url__startswith=EDBOOX).mock(return_value=httpx.Response(200, json=STUDENT))
        ctx = svc.context_for(p)          # nothing stored → refresh scheduled (sync here)
    assert ctx.source == "claims"          # this request never waited on Edboox
    assert svc.context_for(p).source == "edboox_http"   # the refreshed context is used next


def test_edboox_down_keeps_last_known_context(vocab):
    svc, directory = _service(EdbooxHttpContextProvider(
        ContextConfig(provider="edboox_http", base_url=EDBOOX), vocab), ttl_h=0.0001)
    p = _registered(directory)
    old = UserContext(student=StudentContext(grade=10), source="edboox_http",
                      synced_at=datetime.now(UTC) - timedelta(days=3))
    directory.save_context(p.id, old.model_dump(mode="json"))
    with respx.mock() as mock:
        mock.get(url__startswith=EDBOOX).mock(side_effect=httpx.ConnectError("down"))
        ctx = svc.context_for(p)
    assert ctx.student.grade == 10        # stale but kept; nothing raised


def test_claims_fallback_without_any_upstream():
    p = make_principal("student", tenant="sch-7", grade=6)
    ctx = ClaimsContextProvider().fetch(p)
    assert ctx.student.grade == 6 and ctx.school.id == "sch-7"


# ── ScopePolicy ──────────────────────────────────────────────────────────────
STUDENT_CTX = UserContext(student=StudentContext(grade=8))


def test_default_mode_prefills_but_the_user_may_override():
    policy = ScopePolicy(ScopeConfig())
    student = make_principal("student", grade=8)
    filled = policy.conversation_defaults(student, STUDENT_CTX, ChatDefaults())
    assert filled.grade == 8
    chosen = policy.conversation_defaults(student, STUDENT_CTX, ChatDefaults(grade=10))
    assert chosen.grade == 10                                   # explicit choice wins
    turn = policy.turn_scope(student, STUDENT_CTX)
    assert turn.allow_broad_fallback and not turn.locked_filters


def test_locked_mode_forces_scope_and_disables_fallback():
    policy = ScopePolicy(ScopeConfig(roles={"student": ScopeRoleConfig(mode="locked")}))
    student = make_principal("student", grade=8)
    forced = policy.conversation_defaults(student, STUDENT_CTX,
                                          ChatDefaults(grade=12, mode=FilterMode.broad))
    assert forced.grade == 8 and forced.mode == FilterMode.scoped
    turn = policy.turn_scope(student, STUDENT_CTX)
    mode, filters, _ = turn.apply(FilterMode.broad, {"grade": 12, "subject": "Science"}, None)
    assert mode == FilterMode.scoped and filters == {"grade": 8, "subject": "Science"}
    assert turn.allow_broad_fallback is False


def test_off_mode_for_teachers_and_superadmins():
    policy = ScopePolicy(ScopeConfig())
    teacher = make_principal("teacher")
    req = ChatDefaults(grade=3)
    assert policy.conversation_defaults(teacher, STUDENT_CTX, req) == req
    assert policy.mode_for(make_principal("student", superadmin=True)) == "off"


def test_role_collection_allowlist():
    policy = ScopePolicy(ScopeConfig(roles={"student": ScopeRoleConfig(
        mode="default", collections=["reference", "curriculum"])}))
    student = make_principal("student")
    turn = policy.turn_scope(student, STUDENT_CTX)
    assert turn.apply(FilterMode.scoped, {}, ["exemplar", "reference"])[2] == ["reference"]
    assert turn.apply(FilterMode.scoped, {}, None)[2] == ["reference", "curriculum"]
    assert TurnScope().apply(FilterMode.broad, {}, None) == (FilterMode.broad, {}, None)


def test_teacher_assignments_fill_unset_profile_defaults_only():
    ctx = UserContext(teacher=TeacherContext(assignments=[
        TeachingAssignment(grade=8, subject="Science")]), language="ne")
    filled = ScopePolicy.profile_defaults(TeacherProfile(), ctx)
    assert (filled.default_grade, filled.default_subject, filled.default_language) == (
        8, "Science", "ne")
    kept = ScopePolicy.profile_defaults(TeacherProfile(default_grade=6), ctx)
    assert kept.default_grade == 6


# ── context personalises; it never authorizes ────────────────────────────────
def test_context_cannot_change_permissions_or_knowledge_scope(base_settings_dict):
    from lessonforge.knowledge.scope import KnowledgePolicy

    settings = Settings(**base_settings_dict)
    student = make_principal("student", tenant="sch-7")
    rich = UserContext(school={"id": "sch-OTHER"}, teacher=TeacherContext(assignments=[
        TeachingAssignment(grade=10, subject="Science")]))
    before = (student.permissions, KnowledgePolicy(settings.knowledge).scope_for(student))
    assert rich.school.id == "sch-OTHER"          # context claims another school…
    after = (student.permissions, KnowledgePolicy(settings.knowledge).scope_for(student))
    assert before == after                        # …and nothing about access moves
    assert "school:sch-OTHER" not in after[1].spaces


def test_locked_student_chat_ignores_client_scope_end_to_end(base_settings_dict):
    settings = Settings(**{**base_settings_dict, "scope": {
        "bindings": {"student.grade": "grade"},
        "roles": {"student": {"mode": "locked"}, "teacher": {"mode": "off"}}}})
    c = build_container(settings, llm=FakeLLM(response={"answer": "x"}))
    student = make_principal("student", tenant="sch-7", grade=8)
    client = api_client(c, student)
    conv = client.post("/api/v1/chat/conversations",
                       json={"defaults": {"grade": 12, "mode": "broad"}}).json()
    assert conv["defaults"]["grade"] == 8 and conv["defaults"]["mode"] == "scoped"
    cfg = client.get("/api/v1/chat/config").json()
    assert cfg["scope_mode"] == "locked" and cfg["scope_defaults"] == {"grade": 8}
    r = client.post(f"/api/v1/chat/conversations/{conv['id']}/message",
                    json={"message": "what is light?", "mode": "broad", "filters": {"grade": 12}})
    assert r.status_code == 200
    stored = c.get_chat_store().messages(conv["id"])[0]
    assert stored.meta["mode"] == "scoped" and stored.meta["filters"]["grade"] == 8
