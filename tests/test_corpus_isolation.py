"""Knowledge-space isolation (§16): a School-B principal reaches School-A's
private chunk through NO path — and global stays visible to both. Every path is
exercised: narrow/section/chapter retrieval, grounding, table of contents, the
lenient empty-result fallback, corpus listing, and (end to end) the prompts sent
to the LLM by lesson/quiz/unit/chat generation."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from lessonforge import request_context
from lessonforge.auth.principal import Perm
from lessonforge.config import ChatRetrievalConfig, Settings
from lessonforge.domain.chat import FilterMode
from lessonforge.knowledge.scope import AccessScope, KnowledgePolicy
from lessonforge.rag.chunkers import build_chunker
from lessonforge.rag.documents import Collection, Document
from lessonforge.request_context import RequestContext
from lessonforge.services.chat.retrieve import ChatRetriever
from lessonforge.services.chat.transform import TransformedQuery
from tests.conftest import FakeLLM, api_client, build_container, make_principal

SECRET = "ZEBRAQUARTZ"
BOOK = """# Unit 1 Plants
## Photosynthesis
Plants make food from sunlight. {extra}
## Respiration
Plants breathe at night. {extra}
"""


@pytest.fixture
def world(base_settings_dict, valid_ldd_dict):
    settings = Settings(**base_settings_dict)
    llm = FakeLLM(response=valid_ldd_dict)
    c = build_container(settings, llm=llm)
    md = build_chunker("markdown")
    c.ingestor.ingest_documents(Collection.reference, [Document(
        id="global-book", text=BOOK.format(extra="photosynthesis public"), source="CDC Book",
        metadata={"grade": 6, "subject": "Science"})], chunker=md)
    c.ingestor.ingest_documents(Collection.reference, [Document(
        id="a-notes", text=BOOK.format(extra=f"{SECRET} photosynthesis private"),
        source="School A notes", metadata={"grade": 6, "subject": "Science"})],
        chunker=md, space="school:sch-a")
    policy = KnowledgePolicy(settings.knowledge)
    a = make_principal("teacher", tenant="sch-a")
    b = make_principal("teacher", tenant="sch-b")
    return c, llm, policy, a, b


def _as(policy, p):
    return request_context.bound(RequestContext(principal=p, access_scope=policy.scope_for(p)))


def _texts(chunks) -> str:
    return " ".join(getattr(ch, "text", "") for ch in chunks)


@pytest.mark.parametrize("granularity", ["narrow", "section", "broad"])
def test_retrieval_at_every_granularity(world, granularity):
    c, _, policy, a, b = world
    with _as(policy, b):
        hits = c.retriever.retrieve("reference", f"{SECRET} photosynthesis",
                                    granularity=granularity, top_n=20)
    assert SECRET not in _texts(hits) and "public" in _texts(hits)       # global still visible
    with _as(policy, a):
        hits = c.retriever.retrieve("reference", f"{SECRET} photosynthesis",
                                    granularity=granularity, top_n=20)
    assert SECRET in _texts(hits)                                          # positive control


def test_grounding_and_table_of_contents(world):
    c, _, policy, a, b = world
    with _as(policy, b):
        bundle = c.grounding.ground(query=f"{SECRET} photosynthesis", grade=6, subject="Science")
        toc = c.grounding.skeleton("reference", doc_id="school:sch-a/a-notes")
    assert SECRET not in bundle.as_prompt_context() and toc == []
    with _as(policy, a):
        assert c.grounding.skeleton("reference", doc_id="school:sch-a/a-notes")


def test_lenient_fallback_drops_filters_never_the_scope(world):
    c, _, policy, _, b = world
    chat = ChatRetriever(c.retriever, ChatRetrievalConfig(collections=["reference"]))
    q = TransformedQuery(standalone=f"{SECRET} photosynthesis",
                         queries=[f"{SECRET} photosynthesis"])
    with _as(policy, b):     # grade 12 matches nothing → broadened, unfiltered search
        hits = chat.retrieve(q, mode=FilterMode.scoped, filters={"grade": 12})
    assert hits and SECRET not in " ".join(h.text for h in hits)


def test_unbound_reads_default_to_the_shared_shelf_only(world):
    c, *_ = world
    hits = c.retriever.retrieve("reference", f"{SECRET} photosynthesis", top_n=20)
    assert SECRET not in _texts(hits) and "public" in _texts(hits)


def test_same_text_in_two_spaces_never_overwrites(world):
    c, *_ = world
    for space in ("school:sch-x", "school:sch-y"):
        c.ingestor.ingest_records(Collection.pedagogical, [{"text": "identical tip"}], space=space)
    pts = [r for r in c.vector_store.data["pedagogical"] if r.payload["text"] == "identical tip"]
    assert len(pts) == 2 and len({p.id for p in pts}) == 2
    assert {p.payload["_space"] for p in pts} == {"school:sch-x", "school:sch-y"}


def test_record_metadata_cannot_choose_its_own_space(world):
    c, *_ = world
    c.ingestor.ingest_records(Collection.pedagogical,
                              [{"text": "sneaky", "_space": "school:sch-a"}], space="global")
    pt = next(r for r in c.vector_store.data["pedagogical"] if r.payload["text"] == "sneaky")
    assert pt.payload["_space"] == "global"


# ── end to end through the API: nothing of School A reaches B's prompts ──────
API_CALLS = [
    ("post", "/api/v1/lessons/generate", {"topic": f"{SECRET} photosynthesis", "grade": 6,
                                          "subject": "Science", "duration_min": 45}),
    ("post", "/api/v1/artifacts/quiz/generate", {"topic": f"{SECRET} photosynthesis", "grade": 6,
                                                 "subject": "Science"}),
    ("post", "/api/v1/units/plan", {"topic": f"{SECRET} photosynthesis", "grade": 6,
                                    "subject": "Science", "num_days": 2}),
    ("post", "/api/v1/grounding/preview", {"topic": f"{SECRET} photosynthesis", "grade": 6,
                                           "subject": "Science"}),
]


def _seen(world, principal, call) -> bool:
    c, llm, *_ = world
    method, url, body = call
    r = getattr(api_client(c, principal), method)(url, json=body)
    prompts = " ".join(str(x["prompt"]) + str(x["system"]) for x in llm.calls)
    return "private" in prompts or "School A notes" in prompts or "School A notes" in r.text


@pytest.mark.parametrize("call", API_CALLS)
def test_positive_control_school_a_sees_its_own_material(world, call):
    """Without this, the isolation test below could pass vacuously."""
    assert _seen(world, world[3], call)


@pytest.mark.parametrize("call", API_CALLS)
def test_no_api_path_leaks_into_prompts_or_responses(world, call):
    c, llm, _, _, b = world
    client = api_client(c, b)
    method, url, body = call
    r = getattr(client, method)(url, json=body)
    prompts = " ".join(str(x["prompt"]) + str(x["system"]) for x in llm.calls)
    # the topic itself is in the prompt; the private CHUNK text must not be
    assert "private" not in prompts and "School A notes" not in prompts
    assert "School A notes" not in r.text and "private" not in r.text


def test_chat_answer_sources_exclude_other_schools(world):
    c, _, _, _, b = world
    client = api_client(c, b)
    cid = client.post("/api/v1/chat/conversations", json={}).json()["id"]
    r = client.post(f"/api/v1/chat/conversations/{cid}/message",
                    json={"message": f"{SECRET} photosynthesis", "mode": "broad"})
    assert r.status_code == 200
    sources = [json.loads(line[6:]) for line in r.text.splitlines()
               if line.startswith("data: ") and "School" in line or "CDC" in line]
    assert "School A notes" not in r.text and "private" not in r.text
    assert sources or "CDC Book" in r.text                       # global still answers


def test_corpus_listing_is_scoped_for_a_school_curator(world):
    """A future school-admin with corpus.manage sees only its readable spaces."""
    c, _, _, _, b = world
    curator = replace(b, permissions=b.permissions | {Perm.CORPUS_MANAGE})
    client = api_client(c, curator)
    recs = client.get("/api/v1/corpus/collections/reference/records?limit=100").json()["items"]
    assert recs and all(r["space"] in ("global", "school:sch-b") for r in recs)
    assert client.get("/api/v1/corpus/collections/reference/records?space=school:sch-a"
                      ).status_code == 403
    counts = client.get("/api/v1/corpus/overview").json()["collections"]
    total = next(col["count"] for col in counts if col["name"] == "reference")
    admin_total = c.vector_store.count("reference", scope=AccessScope.all())
    assert total < admin_total
    sources = client.get("/api/v1/corpus/sources").json()["items"]
    assert all(s["space"] != "school:sch-a" for s in sources)
    # and it cannot write into another school's shelf
    w = client.post("/api/v1/corpus/ingest", json={"collection": "pedagogical",
                                                   "records": [{"text": "x"}],
                                                   "space": "school:sch-a"})
    assert w.status_code == 403


def test_chat_events_report_which_spaces_answered(world):
    """chat.message.spaces_hit tells Edboox when a school's own notes were used."""
    c, _, _, a, _ = world
    client = api_client(c, a)
    cid = client.post("/api/v1/chat/conversations", json={}).json()["id"]
    client.post(f"/api/v1/chat/conversations/{cid}/message",
                json={"message": f"{SECRET} photosynthesis", "mode": "broad"})
    msg = next(r.event for r in c.platform.activity_reader.feed(
        after=0, limit=50, types=["chat.message"], tenant_id=None).items)
    assert "school:sch-a" in msg.props["spaces_hit"]
