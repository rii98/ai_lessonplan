"""The memory layer: the minors-first policy, extraction, recall, mastery with forgetting,
the per-user switch, owner isolation (every store, Postgres included when
TEST_DATABASE_URL is set), the pipeline integration (structured summary off the request
path, learner notes in answers), the learner's own /me endpoints, and learn mode seeding
from / committing to long-term mastery."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from lessonforge.config import ChatConfig, LongTermMemoryConfig
from lessonforge.domain.chat import ChatMessageRequest, Conversation
from lessonforge.domain.memory import MasteryRecord, MemoryItem, MemoryOp
from lessonforge.providers.base import LLMClient, LLMResult
from lessonforge.rag.retriever import Retriever
from lessonforge.services.chat.context import ContextBuilder
from lessonforge.services.chat.memory import ConversationMemory
from lessonforge.services.chat.pipeline import ChatPipeline
from lessonforge.services.chat.retrieve import ChatRetriever
from lessonforge.services.chat.store import MemoryChatStore, PostgresChatStore
from lessonforge.services.chat.synthesize import AnswerSynthesizer
from lessonforge.services.chat.transform import PassthroughTransformer
from lessonforge.services.learn.store import (
    MemoryLearningSessionStore,
    PostgresLearningSessionStore,
)
from lessonforge.services.memory import InlineJobs, MemoryService
from lessonforge.services.memory.extractor import LLMExtractor, MemoryExtractor
from lessonforge.services.memory.policy import AllowlistPolicy, sensitive
from lessonforge.services.memory.store import MemoryMemoryStore, PostgresMemoryStore
from tests.conftest import make_principal, pg_principal
from tests.learn_fixtures import session as learn_session


# ── policy: data minimisation for minors ─────────────────────────────────────
@pytest.mark.parametrize("text", [
    "My name is Sita", "lives in ward no 4", "my dad is a driver", "has ADHD",
    "is a Brahmin", "got 45 marks in maths", "email me at a@b.com", "call 9841234567",
    "her password is cat", "my friend Ram is bad at maths",
])
def test_sensitive_content_is_never_remembered(text):
    assert sensitive(text)
    assert not AllowlistPolicy().allow(MemoryOp(op="add", kind="preference", content=text, confidence=1), [])


@pytest.mark.parametrize("text", [
    "prefers examples about football", "finds fractions hard", "wants answers in Nepali",
    "is preparing for the SEE exam",
])
def test_learning_facts_are_allowed(text):
    assert AllowlistPolicy().allow(MemoryOp(op="add", kind="learning_need", content=text, confidence=0.9), [])


def test_low_confidence_duplicates_and_foreign_targets_are_vetoed():
    pol = AllowlistPolicy()
    have = [MemoryItem(owner_id="u", kind="interest", content="likes football")]
    assert not pol.allow(MemoryOp(op="add", kind="interest", content="likes football.", confidence=0.9), have)
    assert not pol.allow(MemoryOp(op="add", kind="interest", content="likes cricket", confidence=0.3), have)
    assert not pol.allow(MemoryOp(op="delete", target_id="someone-elses"), have)


# ── extractor ────────────────────────────────────────────────────────────────
class OpsLLM(LLMClient):
    def __init__(self, ops):
        self.ops, self.calls = ops, 0

    def complete(self, prompt, *, system=None, json_schema=None, temperature=None):
        self.calls += 1
        return LLMResult(text=json.dumps({"ops": self.ops}), raw={})

    def health(self):
        return True


def test_extractor_parses_ops_and_skips_trivial_messages():
    llm = OpsLLM([{"op": "add", "kind": "interest", "content": "likes football", "confidence": 0.9},
                  {"op": "nonsense"}, "garbage"])
    ops = LLMExtractor(llm).extract("I love football, use it in examples", [])
    assert [(o.op, o.kind, o.content) for o in ops] == [("add", "interest", "likes football")]
    assert LLMExtractor(llm).extract("ok", []) == [] and llm.calls == 1


# ── stores: same contract, memory and Postgres ───────────────────────────────
@pytest.fixture(params=["memory", "postgres"])
def mem_store(request):
    if request.param == "memory":
        return MemoryMemoryStore(), lambda: make_principal().id
    db = request.getfixturevalue("pg")
    return PostgresMemoryStore(db), lambda: pg_principal(db).id


def test_memory_store_isolates_owners_and_keeps_history(mem_store):
    store, new_owner = mem_store
    a, b = new_owner(), new_owner()
    old = store.add(MemoryItem(owner_id=a, kind="preference", content="prefers English"))
    new = MemoryItem(owner_id=a, kind="preference", content="prefers Nepali")
    assert store.supersede(b, old.id, new) is None                 # not b's
    assert store.supersede(a, old.id, new) is not None
    assert [i.content for i in store.live(a)] == ["prefers Nepali"] and store.live(b) == []
    assert not store.delete(b, new.id) and store.delete(a, new.id)
    store.set_enabled(a, False)
    assert not store.enabled(a) and store.enabled(b)


def test_mastery_upserts_per_owner_and_clear_wipes_it(mem_store):
    store, new_owner = mem_store
    a = new_owner()
    store.upsert_mastery(MasteryRecord(owner_id=a, kc_ref="K1", p_mastery=0.5))
    store.upsert_mastery(MasteryRecord(owner_id=a, kc_ref="K1", p_mastery=0.9, attempts=3))
    store.add(MemoryItem(owner_id=a, kind="goal", content="pass the SEE"))
    [r] = store.mastery(a, ["K1"])
    assert (r.p_mastery, r.attempts) == (pytest.approx(0.9), 3)
    store.clear(a)
    assert store.mastery(a) == [] and store.live(a) == []


@pytest.fixture(params=["memory", "postgres"])
def learn_store(request):
    if request.param == "memory":
        return MemoryLearningSessionStore(), lambda: (make_principal().id, "c1")
    db = request.getfixturevalue("pg")
    chats = PostgresChatStore(db)

    def owner_and_conv():
        owner = pg_principal(db).id
        return owner, chats.create(Conversation(owner_id=owner)).id
    return PostgresLearningSessionStore(db), owner_and_conv


def test_learning_session_store_contract(learn_store):
    from lessonforge.services.learn.store import SessionConflict

    store, owner_and_conv = learn_store
    owner, conv = owner_and_conv()
    s = learn_session()
    s.owner_id, s.conversation_id = owner, conv
    store.create(s)
    a = store.get(s.id, owner_id=owner)
    b = store.get(s.id, owner_id=owner)
    a.states["O1"].attempts = 2
    store.save(a)
    with pytest.raises(SessionConflict):
        store.save(b)                                     # stale write
    assert store.get(s.id, owner_id=owner).states["O1"].attempts == 2
    assert store.current(conv).id == s.id
    other, _ = owner_and_conv()
    assert store.get(s.id, owner_id=other) is None
    a = store.get(s.id, owner_id=owner)
    a.status = "paused"
    store.save(a)
    second = learn_session()
    second.owner_id, second.conversation_id = owner, conv
    store.create(second)                                  # a paused one may coexist
    assert store.current(conv).id == second.id            # the active one wins
    store.log(s.id, {"move": "hint"})


# ── the service ──────────────────────────────────────────────────────────────
class FixedExtractor(MemoryExtractor):
    def __init__(self, ops):
        self.ops = ops

    def extract(self, message, similar):
        return [op(similar) if callable(op) else op for op in self.ops]


def _svc(ops=(), store=None) -> MemoryService:
    return MemoryService(store=store or MemoryMemoryStore(), extractor=FixedExtractor(list(ops)),
                         policy=AllowlistPolicy(), jobs=InlineJobs(), config=LongTermMemoryConfig())


def test_remembering_a_turn_adds_then_updates_instead_of_duplicating():
    svc = _svc([MemoryOp(op="add", kind="preference", content="wants answers in English", confidence=0.9)])
    svc.remember_turn("u", "c", "please answer in English from now on")
    assert len(svc.list("u")) == 1
    svc.extractor = FixedExtractor([lambda similar: MemoryOp(
        op="update", target_id=similar[0].id, kind="preference", content="wants answers in Nepali",
        confidence=0.9)])
    svc.remember_turn("u", "c", "actually, answer in Nepali please")
    assert [i.content for i in svc.list("u")] == ["wants answers in Nepali"]
    assert svc.list("u")[0].source["conversation_id"] == "c"


def test_switching_memory_off_stops_writes_and_reads():
    svc = _svc([MemoryOp(op="add", kind="interest", content="likes football", confidence=0.9)])
    svc.set_enabled("u", False)
    svc.remember_turn("u", "c", "I love football so much")
    assert svc.list("u") == [] and svc.notes_for("u", "football") == []


def test_notes_are_a_profile_slice_plus_relevant_items():
    store = MemoryMemoryStore()
    svc = _svc(store=store)
    store.add(MemoryItem(owner_id="u", kind="preference", content="likes short answers"))
    store.add(MemoryItem(owner_id="u", kind="episode", content="Studied photosynthesis: found light reactions hard"))
    store.add(MemoryItem(owner_id="u", kind="episode", content="Studied fractions: mastered 3/3 parts"))
    notes = svc.notes_for("u", "how does photosynthesis work?")
    assert "likes short answers" in notes and any("photosynthesis" in n for n in notes)
    assert not any("fractions" in n for n in notes)


def test_finishing_a_lesson_commits_mastery_and_an_episode():
    svc = _svc()
    s = learn_session()
    s.states["O1"].status, s.states["O1"].p_mastery, s.states["O1"].checks_passed = "mastered", 0.9, 1
    s.states["O2"].status, s.states["O2"].attempts = "active", 2
    s.states["O2"].misconceptions_seen = ["x"]
    svc.commit_session(s)
    m = {r.kc_ref: r for r in svc.mastery(s.owner_id)}
    assert m["SEED-SC7-PS-1"].stability == 2.5 and m["SEED-SC7-PS-1"].p_mastery == pytest.approx(0.9)
    assert any(k.startswith("free:photosynthesis/") for k in m)        # no standard → stable slug
    assert len(m) == 2                                                  # O3 never reached
    [ep] = [i for i in svc.list(s.owner_id) if i.kind == "episode"]
    assert "mastered 1/3" in ep.content and "found hard" in ep.content and ep.expires_at


def test_prior_mastery_is_discounted_by_forgetting():
    svc = _svc()
    svc.store.upsert_mastery(MasteryRecord(owner_id="u", kc_ref="K", p_mastery=0.9, stability=2.0,
                                           last_seen=datetime.now(UTC) - timedelta(days=20)))
    assert svc.prior("u", ["K"])["K"] < 0.5


# ── pipeline integration ─────────────────────────────────────────────────────
class EchoLLM(LLMClient):
    def __init__(self):
        self.prompts, self.systems = [], []

    def complete(self, prompt, *, system=None, json_schema=None, temperature=None):
        self.systems.append(system or "")
        return LLMResult(text="Goals: learn\nFacts established: x", raw={})

    def stream(self, prompt, *, system=None, temperature=None):
        self.prompts.append(prompt)
        yield "answer"

    def health(self):
        return True


def test_qa_answers_see_learner_notes_and_summaries_use_sections(fake_embedder, fake_store, fake_reranker):
    llm = EchoLLM()
    config = ChatConfig(memory={"window_turns": 1, "summary_trigger_turns": 1})
    config.retrieval.collections = []
    store = MemoryChatStore()
    mstore = MemoryMemoryStore()
    long_term = _svc(store=mstore)
    retr = Retriever(embedder=fake_embedder, vector_store=fake_store, reranker=fake_reranker, hybrid=False)
    pipe = ChatPipeline(store=store, memory=ConversationMemory(store, config.memory, llm=llm),
                        transformer=PassthroughTransformer(), retriever=ChatRetriever(retr, config.retrieval),
                        context_builder=ContextBuilder(config.context),
                        synthesizer=AnswerSynthesizer(llm, config.synthesis), config=config,
                        jobs=InlineJobs(), long_term=long_term)
    conv = store.create(Conversation(owner_id="u"))
    mstore.add(MemoryItem(owner_id="u", kind="preference", content="likes football examples"))
    for q in ("what is force?", "and friction?"):
        list(pipe.stream(store.get(conv.id, owner_id="u"), ChatMessageRequest(message=q)))
    assert "likes football examples" in llm.prompts[-1]
    assert any("Facts established" in s for s in llm.systems)          # structured summary
    assert store.get(conv.id, owner_id="u").summary.startswith("Goals:")


# ── /me endpoints + learn mode ↔ mastery ─────────────────────────────────────
@pytest.fixture
def client(base_settings_dict):
    from lessonforge.config import Settings
    from lessonforge.container import Container
    from lessonforge.export import ExportService
    from lessonforge.rag.grounding import GroundingRetriever
    from lessonforge.services.generation import LessonGenerator
    from lessonforge.services.intake import HeuristicIntake
    from tests.conftest import FakeEmbedder, FakeReranker, FakeVectorStore, api_client
    from tests.learn_fixtures import LearnLLM

    llm = LearnLLM()
    emb, vs, rr = FakeEmbedder(), FakeVectorStore(), FakeReranker()
    retr = Retriever(embedder=emb, vector_store=vs, reranker=rr)
    settings = Settings(**{**base_settings_dict, "chat": {
        "query_transform": {"provider": "passthrough"}, "retrieval": {"collections": []},
        "memory": {"jobs": "inline", "long_term": {"extractor": "none"}}}})
    grounding = GroundingRetriever(retr, settings.grounding)
    c = Container(settings=settings, llm=llm, llm_fast=llm, embedder=emb, reranker=rr,
                  vector_store=vs, retriever=retr, grounding=grounding,
                  generator=LessonGenerator(llm=llm, grounding=grounding),
                  exporter=ExportService(settings.export), intake=HeuristicIntake())
    return api_client(c), c


def test_me_endpoints_list_forget_and_switch_off(client):
    http, c = client
    svc = c.get_memory_service()
    me = http.get("/api/v1/me").json()["id"]
    item = svc.store.add(MemoryItem(owner_id=me, kind="interest", content="likes football"))
    body = http.get("/api/v1/me/memories").json()
    assert body["enabled"] and [i["content"] for i in body["items"]] == ["likes football"]
    assert http.delete(f"/api/v1/me/memories/{item.id}").json() == {"deleted": True}
    assert http.delete(f"/api/v1/me/memories/{item.id}").status_code == 404
    assert http.put("/api/v1/me/memory-settings", json={"enabled": False}).json() == {"enabled": False}
    assert http.get("/api/v1/me/memories").json()["enabled"] is False


def test_ending_a_lesson_feeds_mastery_and_a_new_lesson_starts_from_it(client):
    from tests.test_learn_api import _new, _start

    http, c = client
    cid = _new(http)
    _start(http, cid)
    sid = http.get(f"/api/v1/chat/conversations/{cid}/learning-session").json()["session"]["id"]
    learn = c.get_chat_pipeline().learn
    s = learn.store.get(sid, owner_id=learn.store.current(cid).owner_id)
    s.states["O1"].status, s.states["O1"].p_mastery, s.states["O1"].checks_passed = "mastered", 0.95, 1
    learn.store.save(s)
    assert http.post(f"/api/v1/chat/learning-sessions/{sid}/end").json()["status"] == "ended"
    progress = http.get("/api/v1/me/learning").json()["items"]
    # no curriculum evidence in this test → the stable "free:" key (same on every lesson)
    assert progress[0]["kc_ref"] == "free:photosynthesis/plants-need-sunlight"
    assert progress[0]["mastery"] > 0.9
    # the next lesson on the same topic opens by verifying, not re-teaching
    cid2 = _new(http)
    events = _start(http, cid2)
    assert [d for e, d in events if e == "done"][-1]["learn"]["move"] == "skip_check"


def test_forget_erases_a_learners_memories_mastery_and_lessons(pg):
    """`lessonforge-admin forget` deletes the principal; the FK cascade must take every
    learn-mode and memory row with it (no separate code path to forget to update)."""
    from lessonforge.auth.directory import PostgresPrincipalDirectory

    p = pg_principal(pg)
    mem, lessons = PostgresMemoryStore(pg), PostgresLearningSessionStore(pg)
    conv = PostgresChatStore(pg).create(Conversation(owner_id=p.id))
    mem.add(MemoryItem(owner_id=p.id, kind="interest", content="likes football"))
    mem.upsert_mastery(MasteryRecord(owner_id=p.id, kc_ref="K", p_mastery=0.5))
    mem.set_enabled(p.id, True)
    s = learn_session()
    s.owner_id, s.conversation_id = p.id, conv.id
    lessons.create(s)
    lessons.log(s.id, {"move": "hint"})
    assert PostgresPrincipalDirectory(pg).delete(p.issuer, p.subject)
    with pg.tx() as cur:
        for table in ("user_memories", "mastery_records", "memory_settings",
                      "learning_sessions", "learning_events"):
            cur.execute(f"SELECT count(*) AS n FROM {table}")
            assert cur.fetchone()["n"] == 0, table
