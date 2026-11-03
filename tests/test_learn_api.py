"""Learn mode over HTTP (SSE), end to end with fakes: start a session → the plan's first
move → typed replies go to the tutor (not QA) → quick actions → an inline widget graded
server-side → advancing (lazy detail) → pause frees the chat → resume → end. Plus the
router's door (offer vs start) and the leak guard's fallback."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from lessonforge.config import Settings
from lessonforge.container import Container
from lessonforge.export import ExportService
from lessonforge.rag.grounding import GroundingRetriever
from lessonforge.rag.retriever import Retriever
from lessonforge.services.generation import LessonGenerator
from lessonforge.services.intake import HeuristicIntake
from tests.conftest import FakeEmbedder, FakeReranker, FakeVectorStore, api_client
from tests.learn_fixtures import LearnLLM
from tests.test_chat_artifacts_api import _sse

API = "/api/v1/chat"


def _client(base_settings_dict, llm) -> TestClient:
    emb, store, rr = FakeEmbedder(), FakeVectorStore(), FakeReranker()
    retr = Retriever(embedder=emb, vector_store=store, reranker=rr)
    settings = Settings(**{**base_settings_dict, "chat": {
        "query_transform": {"provider": "passthrough"}, "retrieval": {"collections": []},
        "quiz": {"llm_grading": False}, "learn": {"tracer": "hybrid"}}})
    grounding = GroundingRetriever(retr, settings.grounding)
    c = Container(settings=settings, llm=llm, llm_fast=llm, embedder=emb, reranker=rr,
                  vector_store=store, retriever=retr, grounding=grounding,
                  generator=LessonGenerator(llm=llm, grounding=grounding),
                  exporter=ExportService(settings.export), intake=HeuristicIntake())
    return api_client(c)


@pytest.fixture
def llm():
    return LearnLLM()


@pytest.fixture
def client(base_settings_dict, llm):
    return _client(base_settings_dict, llm)


def _new(client) -> str:
    return client.post(f"{API}/conversations", json={}).json()["id"]


def _start(client, cid, topic="photosynthesis"):
    return _sse(client.post(f"{API}/conversations/{cid}/learning-sessions", json={"topic": topic}).text)


def _say(client, cid, text):
    return _sse(client.post(f"{API}/conversations/{cid}/message", json={"message": text}).text)


def _act(client, cid, action, value=None):
    return _sse(client.post(f"{API}/conversations/{cid}/learn",
                            json={"action": action, "action_value": value}).text)


def _answer(client, cid, widget_id, response):
    return _sse(client.post(f"{API}/conversations/{cid}/learn",
                            json={"widget_response": {"widget_id": widget_id, "response": response}}).text)


def _types(events):
    return [e for e, _ in events]


def _last(events, kind):
    return [d for e, d in events if e == kind][-1]


def test_starting_plans_once_then_opens_with_a_hook(client, llm):
    cid = _new(client)
    events = _start(client, cid)
    assert _types(events) == ["intent", "status", "status", "token", "learn_state", "done"]
    state = _last(events, "learn_state")
    assert (state["status"], state["cursor"], state["total"]) == ("active", "O1", 2)
    assert "energy to make food" not in str(events)            # hidden material never ships
    assert llm.calls[:2] == ["outline", "detail:O1"]           # lazy: only O1 detailed
    assert _last(events, "done")["learn"]["move"] == "hook"


def test_typed_replies_go_to_the_tutor_not_to_qa(client, llm):
    cid = _new(client)
    _start(client, cid)
    events = _say(client, cid, "maybe it eats the soil and rocks")
    assert "sources" not in _types(events) and "learn_state" in _types(events)
    assert "trace" in llm.calls and "route" not in llm.calls   # one classification, no router


def test_a_widget_is_graded_server_side_and_advancing_details_the_next_part(client, llm):
    cid = _new(client)
    _start(client, cid)
    events = _act(client, cid, "skip")                          # "I know this" → verify
    w = _last(events, "learn_widget")
    assert w["purpose"] == "skip" and "answer" not in w and w["choices"]
    right = w["choices"].index("energy to make food")
    events = _answer(client, cid, w["id"], right)
    fb = _last(events, "learn_feedback")
    assert fb["correct"] is True and fb["expected"] == "energy to make food"
    state = _last(events, "learn_state")
    assert state["cursor"] == "O2" and state["objectives"][0]["status"] in ("mastered", "skipped")
    assert "detail:O2" in llm.calls
    # the transcript reads naturally
    msgs = client.get(f"{API}/conversations/{cid}").json()["messages"]
    assert "Answered: energy to make food" in [m["content"] for m in msgs]
    # replaying the same answer is refused, kindly
    again = _answer(client, cid, w["id"], right)
    assert "already been answered" in "".join(d for e, d in again if e == "token")


def test_pause_frees_the_chat_and_resume_continues(client):
    cid = _new(client)
    _start(client, cid)
    assert _last(_act(client, cid, "pause"), "learn_state")["status"] == "paused"
    assert _types(_say(client, cid, "what is a cell?")) == ["token", "sources", "done"]  # ordinary QA
    cur = client.get(f"{API}/conversations/{cid}/learning-session").json()
    assert cur["session"]["status"] == "paused"
    assert _last(_act(client, cid, "resume"), "learn_state")["status"] == "active"


def test_learn_input_without_a_session_is_an_error_not_a_crash(client):
    cid = _new(client)
    events = _act(client, cid, "hint")
    assert _types(events) == ["error"] and events[0][1]["code"] == "no_session"
    assert client.post(f"{API}/conversations/{cid}/learn", json={}).status_code == 422


def test_the_leak_guard_falls_back_when_the_model_gives_the_answer_away(base_settings_dict):
    llm = LearnLLM(reply="Easy: it is energy to make food!")
    client = _client(base_settings_dict, llm)
    cid = _new(client)
    _start(client, cid)
    events = _act(client, cid, "skip")
    said = "".join(d for e, d in events if e == "token")
    assert "energy to make food" not in said and _last(events, "learn_widget")
    assert llm.calls.count("tutor") >= 3                       # draft + strict retry, then template


def test_the_router_offers_a_lesson_or_starts_one_when_asked_plainly(client, llm):
    cid = _new(client)
    events = _say(client, cid, "help me understand photosynthesis please")
    assert "learn_state" in _types(events)                       # explicit → started
    cid2 = _new(client)
    llm_route = llm.complete

    def routed(prompt, **kw):
        if "intent router" in (kw.get("system") or "") and "struggling" in prompt:
            from lessonforge.providers.base import LLMResult
            return LLMResult(text='{"intent": "learn", "confidence": 0.9, "slots": {"topic": "fractions"}}', raw={})
        return llm_route(prompt, **kw)

    llm.complete = routed
    events = _say(client, cid2, "I'm struggling with fractions")
    assert _last(events, "learn_offer") == {"topic": "fractions"}
    assert "learn_state" not in _types(events)                   # offered, not forced


def test_ending_a_session_from_the_api(client):
    cid = _new(client)
    _start(client, cid)
    sid = client.get(f"{API}/conversations/{cid}/learning-session").json()["session"]["id"]
    assert client.post(f"{API}/learning-sessions/{sid}/end").json()["status"] == "ended"
    assert client.get(f"{API}/conversations/{cid}/learning-session").json()["session"] is None
    assert client.post(f"{API}/learning-sessions/not-a-session/end").status_code == 404


def test_typing_stop_wraps_up(client):
    cid = _new(client)
    _start(client, cid)
    events = _say(client, cid, "stop")
    assert _last(events, "learn_state")["status"] == "ended"
    assert _last(events, "done")["learn"]["move"] == "wrap_up"


def test_regression_no_check_is_repeated_and_answers_are_per_display(client):
    """The loop report: a missed check then a passed one must FINISH the objective — never
    cycle the same two checks — and every widget display has its own id."""
    cid = _new(client)
    _start(client, cid)
    w1 = _last(_act(client, cid, "skip"), "learn_widget")             # verify prior knowledge
    assert w1["id"] != w1["check_id"]                                  # a per-display id
    wrong = next(i for i, c in enumerate(w1["choices"]) if c != "energy to make food")
    _answer(client, cid, w1["id"], wrong)                              # missed → taught
    events = _act(client, cid, "faster")
    w2 = _last(events, "learn_widget")
    assert w2["check_id"] != w1["check_id"]                            # never the same check
    assert w2["widget"] == "order"                                     # the plan's other check
    letter = {it["text"]: it["letter"] for it in w2["items"]}
    events = _answer(client, cid, w2["id"], [letter["light hits leaf"], letter["energy captured"]])
    assert _last(events, "learn_feedback")["correct"] is True
    state = _last(events, "learn_state")
    assert state["cursor"] == "O2" or state["status"] == "completed"   # moved on — no loop
    # an answer keyed by the CHECK id (not the display) is refused
    stale = _answer(client, cid, w1["check_id"], 1)
    assert "already been answered" in "".join(d for e, d in stale if e == "token")
