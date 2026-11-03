"""Memory v2 (docs/MEMORY.md): the reconciler, the atomic per-learner apply (memory AND
Postgres, including a cross-thread race), versioned recall caching, the digest and its
invalidation, hybrid recall, eviction, retention and re-embedding, the learner's own
remember/edit endpoints, a randomized invariant simulation, and (RUN_INTEGRATION=1) the
real embedder's calibration."""

from __future__ import annotations

import json
import os
import random
import threading
import zlib
from datetime import UTC, datetime, timedelta

import pytest

from lessonforge.config import LongTermMemoryConfig
from lessonforge.domain.memory import Candidate, MemoryItem, MemoryOp
from lessonforge.providers.base import Embedder, LLMClient, LLMResult
from lessonforge.services.memory import InlineJobs, MemoryRefused, MemoryService
from lessonforge.services.memory.extractor import MemoryExtractor
from lessonforge.services.memory.policy import AllowlistPolicy
from lessonforge.services.memory.profile import ProfileSynthesizer
from lessonforge.services.memory.reconciler import LLMReconciler, RulesReconciler
from lessonforge.services.memory.store import Change, MemoryMemoryStore, PostgresMemoryStore, norm
from lessonforge.util import content_terms
from tests.conftest import make_principal, pg_principal
from tests.test_memory_layer import client  # noqa: F401 - the shared API fixture


# ── fakes ────────────────────────────────────────────────────────────────────
class TermEmbedder(Embedder):
    """Bag-of-content-terms vectors: cosine tracks shared meaning words, so dense recall
    is testable without a model."""

    def __init__(self, dim: int = 64, name: str = "terms") -> None:
        self._dim, self.model_name, self.calls = dim, name, 0

    @property
    def dim(self):
        return self._dim

    def embed(self, texts):
        self.calls += 1
        out = []
        for t in texts:
            v = [0.0] * self._dim
            for term in content_terms(t):
                v[zlib.crc32(term.encode()) % self._dim] += 1.0
            out.append(v)
        return out


class ScriptLLM(LLMClient):
    def __init__(self, *replies):
        self.replies, self.calls, self.prompts = list(replies), 0, []

    def complete(self, prompt, *, system=None, json_schema=None, temperature=None):
        self.calls += 1
        self.prompts.append(prompt)
        r = self.replies.pop(0) if self.replies else "{}"
        return LLMResult(text=r if isinstance(r, str) else json.dumps(r), raw={})

    def health(self):
        return True


class FixedExtractor(MemoryExtractor):
    def __init__(self, cands=()):
        self.cands = list(cands)

    def extract(self, message, context=()):
        return list(self.cands)


class CountingStore(MemoryMemoryStore):
    def __init__(self):
        super().__init__()
        self.live_calls = 0

    def live(self, owner_id):
        self.live_calls += 1
        return super().live(owner_id)


class BulletSynth(ProfileSynthesizer):
    def __init__(self, extra: str = ""):
        self.calls, self.extra = 0, extra

    def synthesize(self, items, *, max_chars):
        self.calls += 1
        return "\n".join([f"- {i.content}" for i in items] + ([f"- {self.extra}"] if self.extra else []))


def svc_with(store=None, *, cands=(), embedder=None, reconciler=None, synth=None, **cfg) -> MemoryService:
    return MemoryService(store=store or MemoryMemoryStore(), extractor=FixedExtractor(cands),
                         policy=AllowlistPolicy(), jobs=InlineJobs(),
                         config=LongTermMemoryConfig(**cfg), embedder=embedder,
                         reconciler=reconciler, synthesizer=synth)


def item(owner, content, kind="preference", **kw) -> MemoryItem:
    return MemoryItem(owner_id=owner, kind=kind, content=content, **kw)


def C(content, kind="preference", **kw) -> Candidate:
    return Candidate(kind=kind, content=content, confidence=kw.pop("confidence", 0.9), **kw)


# ── reconciler ───────────────────────────────────────────────────────────────
def test_no_neighbours_means_plain_adds_without_a_model_call():
    llm = ScriptLLM()
    ops = LLMReconciler(llm).reconcile([C("likes football")], [[]])
    assert [(o.op, o.content) for o in ops] == [("add", "likes football")] and llm.calls == 0


def test_llm_ops_are_validated_against_aliases_never_real_ids():
    m1 = item("u", "wants answers in Nepali")
    m2 = item("u", "likes cricket", kind="interest")
    pinned = item("u", "always use short answers", source={"explicit": True})
    llm = ScriptLLM({"operations": [
        {"candidate": "c1", "op": "delete", "target": "m1"},          # alias of retire
        {"candidate": "c1", "op": "add"},
        {"candidate": "c2", "op": "update", "target": "m2", "content": "likes cricket statistics"},
        {"candidate": "c2", "op": "update", "target": m2.id},           # a real id is not an alias
        {"candidate": "c9", "op": "add"},                               # invented candidate
        {"candidate": "c3", "op": "retire", "target": "m3"},            # explicit: not by inference
        "garbage"]})
    ops = LLMReconciler(llm).reconcile(
        [C("wants answers in English"), C("likes cricket stats", kind="interest"), C("prefers long answers")],
        [[(m1, 0.85)], [(m2, 0.8)], [(pinned, 0.7)]])
    assert [(o.op, o.target_id, o.content) for o in ops] == [
        ("retire", m1.id, ""), ("add", None, "wants answers in English"),
        ("update", m2.id, "likes cricket statistics"), ("add", None, "likes cricket stats")]
    assert m1.id not in llm.prompts[0]                                  # ids never shown


def test_malformed_output_is_retried_then_degrades_to_rules():
    known = item("u", "likes football")
    llm = ScriptLLM("not json", '{"ops": "wrong"}')
    ops = LLMReconciler(llm, fallback=RulesReconciler(duplicate_similarity=0.9)).reconcile(
        [C("likes football"), C("finds fractions hard", kind="learning_need")], [[(known, 0.95)], [(known, 0.1)]])
    assert llm.calls == 2
    assert [(o.op, o.content) for o in ops] == [("add", "finds fractions hard")]


# ── store: one atomic apply, both backends ───────────────────────────────────
@pytest.fixture(params=["memory", "postgres"])
def store(request):
    if request.param == "memory":
        return MemoryMemoryStore(), lambda: make_principal().id
    db = request.getfixturevalue("pg")
    return PostgresMemoryStore(db), lambda: pg_principal(db).id


def test_apply_adds_dedupes_supersedes_retires_and_versions(store):
    s, new_owner = store
    a = new_owner()
    r = s.apply(a, [Change("add", item=item(a, "wants answers in Nepali")),
                    Change("add", item=item(a, "Wants answers in Nepali.")),      # same fact
                    Change("add", item=item(a, "likes momo", kind="interest"))])
    assert len(r.added) == 2 and r.skipped == 1
    st = s.state(a)
    assert (st.version, st.removal_version) == (1, 0)                         # adds remove nothing
    nep = next(i for i in s.live(a) if "Nepali" in i.content)
    r = s.apply(a, [Change("update", item=item(a, "wants answers in English", importance=0.2),
                           target_id=nep.id)])
    [eng] = r.updated
    assert eng.importance == 0.5                                              # never lowered by a rewrite
    # a decision made on the old snapshot is stale now: dropped, not resurrected
    stale = s.apply(a, [Change("update", item=item(a, "wants answers in Hindi"), target_id=nep.id)])
    assert stale.skipped == 1 and not stale.changed
    s.apply(a, [Change("retire", target_id=eng.id)])
    assert [i.content for i in s.live(a)] == ["likes momo"]
    st = s.state(a)
    assert st.version == 3 and st.removal_version == 3


def test_at_the_cap_the_least_valuable_note_is_evicted_never_an_explicit_one(store):
    s, new_owner = store
    a = new_owner()
    s.apply(a, [Change("add", item=item(a, "always answer in Nepali", importance=0.1,
                                        source={"explicit": True})),
                Change("add", item=item(a, "likes momo", kind="interest", importance=0.3)),
                Change("add", item=item(a, "likes cricket", kind="interest", importance=0.6))], max_items=3)
    r = s.apply(a, [Change("add", item=item(a, "finds fractions hard", kind="learning_need", importance=0.2))],
                max_items=3)
    assert r.skipped == 1 and not r.changed                                    # less important than any victim
    r = s.apply(a, [Change("add", item=item(a, "preparing for SEE", kind="goal", importance=0.9))], max_items=3)
    assert len(r.evicted) == 1
    assert sorted(i.content for i in s.live(a)) == ["always answer in Nepali", "likes cricket", "preparing for SEE"]


def test_forget_deletes_the_history_chain_and_digests_never_go_backwards(store):
    s, new_owner = store
    a = new_owner()
    [old] = s.apply(a, [Change("add", item=item(a, "wants answers in Nepali"))]).added
    [new] = s.apply(a, [Change("update", item=item(a, "wants answers in English"), target_id=old.id)]).updated
    s.save_profile(a, "- wants answers in English", s.state(a).version)
    s.save_profile(a, "- an older digest", 0)                                 # must not overwrite
    assert s.state(a).profile.content == "- wants answers in English" and s.state(a).digest_valid
    assert s.delete(a, new.id) and not s.delete(a, new.id)
    assert not s.state(a).digest_valid                                       # may mention what was forgotten
    assert s.prune(retention_days=0, now=datetime.now(UTC) + timedelta(days=1)) == 0   # nothing left behind


def test_prune_and_reembedding_upkeep(store):
    s, new_owner = store
    a = new_owner()
    [x] = s.apply(a, [Change("add", item=item(a, "likes momo", kind="interest",
                                              embedding=[1.0, 0.0], embedding_model="old:2"))]).added
    s.apply(a, [Change("update", item=item(a, "likes momo and chowmein", kind="interest"), target_id=x.id)])
    s.add(item(a, "Studied ammonia", kind="episode", expires_at=datetime.now(UTC) - timedelta(days=40)))
    assert s.prune(retention_days=30, now=datetime.now(UTC) + timedelta(days=31)) == 2   # history + expired
    [stale] = s.stale_embeddings("new:2")
    v = s.state(a).version
    s.set_embedding(a, stale.id, [0.0, 1.0], "new:2")
    assert s.stale_embeddings("new:2") == [] and s.state(a).version == v + 1


def test_concurrent_writers_never_duplicate_a_fact(pg):
    """Two replicas (here: threads) ingesting the same fact at once must converge on one
    row: the per-learner advisory lock + apply-time re-validation."""
    s = PostgresMemoryStore(pg)
    a = pg_principal(pg).id
    barrier = threading.Barrier(6)

    def write():
        barrier.wait()
        s.apply(a, [Change("add", item=item(a, "likes football examples", kind="interest"))])
    threads = [threading.Thread(target=write) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert [i.content for i in s.live(a)] == ["likes football examples"]
    assert s.state(a).version == 1


def test_forget_cascade_includes_the_digest(pg):
    from lessonforge.auth.directory import PostgresPrincipalDirectory

    p = pg_principal(pg)
    s = PostgresMemoryStore(pg)
    s.add(item(p.id, "likes football"))
    s.save_profile(p.id, "- likes football", 1)
    assert PostgresPrincipalDirectory(pg).delete(p.issuer, p.subject)
    with pg.tx() as cur:
        cur.execute("SELECT count(*) AS n FROM memory_profiles")
        assert cur.fetchone()["n"] == 0


# ── service: write pipeline ──────────────────────────────────────────────────
def test_sensitive_candidates_never_reach_the_reconciling_model():
    llm = ScriptLLM()
    svc = svc_with(cands=[C("my dad is a driver"), C("has ADHD", kind="learning_need")],
                   reconciler=LLMReconciler(llm))
    svc.store.add(item("u", "likes momo", kind="interest"))
    svc.remember_turn("u", "c", "my dad is a driver and I have ADHD")
    assert llm.calls == 0 and [i.content for i in svc.list("u")] == ["likes momo"]


def test_a_reversed_preference_retires_the_old_one_through_the_reconciler():
    emb = TermEmbedder()
    llm = ScriptLLM({"operations": [{"candidate": "c1", "op": "retire", "target": "m1"},
                                    {"candidate": "c1", "op": "add"}]})
    svc = svc_with(cands=[C("does not want football examples")], embedder=emb,
                   reconciler=LLMReconciler(llm))
    svc.ingest("u", [C("wants football examples")], {})
    svc.remember_turn("u", "c", "stop using football examples please")
    assert [i.content for i in svc.list("u")] == ["does not want football examples"]
    assert "m1: [preference] wants football examples" in llm.prompts[0]   # neighbour of the CANDIDATE


def test_learner_added_notes_are_policy_checked_reconciled_and_editable():
    svc = svc_with(embedder=TermEmbedder())
    [n] = svc.remember("u", "use cricket examples", kind="interest")
    assert n.origin == "you" and n.importance == 1.0
    with pytest.raises(MemoryRefused, match="already"):
        svc.remember("u", "Use cricket examples.", kind="interest")
    with pytest.raises(MemoryRefused, match="sensitive"):
        svc.remember("u", "my phone is 9841234567")
    edited = svc.edit("u", n.id, "use cricket and football examples")
    assert edited.source.get("explicit") and [i.content for i in svc.list("u")] == [edited.content]
    with pytest.raises(KeyError):
        svc.edit("u", n.id, "stale id")


# ── service: read path ───────────────────────────────────────────────────────
def test_recall_reads_rows_only_when_the_version_changes():
    store = CountingStore()
    svc = svc_with(store)
    store.add(item("u", "likes short answers"))
    for _ in range(5):
        svc.recall("u", "what is force?")
    assert store.live_calls == 1
    svc.ingest("u", [C("likes football examples", kind="interest")], {})   # bump → one re-read
    for _ in range(3):
        assert "likes football examples" in svc.recall("u", "q").notes()
    assert store.live_calls == 2


def test_relevant_notes_are_hybrid_ranked_diverse_and_budgeted():
    emb = TermEmbedder()
    svc = svc_with(embedder=emb, profile_kinds=["preference"])
    svc.ingest("u", [C("likes short answers")], {})
    for text in ("Studied photosynthesis: found light reactions hard",
                 "Studied photosynthesis: found light reactions very hard",     # near-duplicate
                 "Studied fractions: mastered 3/3 parts"):
        svc.store.add(item("u", text, kind="episode", embedding=emb.embed([text])[0],
                           embedding_model=svc.model))
    ctx = svc.recall("u", "how does photosynthesis work?")
    assert ctx.profile == ["likes short answers"]
    assert len(ctx.relevant) == 1 and "photosynthesis" in ctx.relevant[0]
    tight = svc_with(svc.store, embedder=emb, profile_kinds=["preference"], recall={"budget_chars": 25})
    assert sum(len(x) + 1 for x in tight.recall("u", "photosynthesis").notes()) <= 25


def test_vectors_from_another_embedder_are_never_compared_and_maintenance_heals_them():
    old, new = TermEmbedder(name="old"), TermEmbedder(dim=32, name="new")
    store = MemoryMemoryStore()
    svc_with(store, embedder=old).ingest("u", [C("likes cricket examples", kind="interest")], {})
    svc = svc_with(store, embedder=new)
    [it] = store.live("u")
    assert it.embedding_model == "old:64"
    assert svc.maintain() == {"pruned": 0, "reembedded": 1}
    assert store.live("u")[0].embedding_model == "new:32"


def test_the_digest_is_used_hidden_on_forget_and_rebuilt_without_the_forgotten_note():
    synth = BulletSynth(extra="has a phone number 9841234567")     # model output is policy-checked too
    svc = svc_with(synth=synth, profile_refresh_writes=1, embedder=TermEmbedder())
    for text in ("likes short answers", "wants answers in Nepali"):
        svc.ingest("u", [C(text)], {})
    ctx = svc.recall("u")
    assert ctx.digest and "phone" not in " ".join(ctx.profile)
    assert set(ctx.profile) == {"likes short answers", "wants answers in Nepali"}
    nep = next(i for i in svc.list("u") if "Nepali" in i.content)
    calls = synth.calls
    svc.forget("u", nep.id)
    assert synth.calls == calls + 1                                 # rebuilt (inline jobs)
    assert "Nepali" not in " ".join(svc.recall("u").notes())
    # a note added after the digest was built still shows, without waiting for a rebuild
    svc.config.profile_refresh_writes = 99
    svc.ingest("u", [C("likes momo examples", kind="interest")], {})
    assert "likes momo examples" in svc.recall("u").profile


# ── invariants under random use ──────────────────────────────────────────────
POOL = ["likes football examples", "likes cricket examples", "wants answers in Nepali",
        "wants answers in English", "finds fractions hard", "finds algebra hard", "likes momo",
        "is preparing for the SEE exam", "prefers short answers", "prefers diagrams"]
KIND = {"finds": "learning_need", "likes": "interest", "is": "goal"}


class ChaosReconciler(RulesReconciler):
    """Like an unreliable model: sometimes updates or retires a random neighbour."""

    def __init__(self, rng):
        super().__init__(duplicate_similarity=0.95)
        self.rng = rng

    def reconcile(self, candidates, neighbours):
        ops = []
        for c, hits in zip(candidates, neighbours, strict=True):
            roll = self.rng.random()
            if hits and roll < 0.3:
                ops.append(MemoryOp(op="update", target_id=hits[0][0].id, kind=c.kind, content=c.content))
            elif hits and roll < 0.45:
                ops += [MemoryOp(op="retire", target_id=hits[0][0].id), MemoryOp(op="add", kind=c.kind, content=c.content)]
            else:
                ops += super().reconcile([c], [hits])
        return ops


@pytest.mark.parametrize("seed", range(6))
def test_memory_invariants_hold_under_random_use(seed):
    rng = random.Random(seed)
    synth = BulletSynth()
    svc = svc_with(embedder=TermEmbedder(), reconciler=ChaosReconciler(rng), synth=synth,
                   max_items_per_user=5, profile_refresh_writes=2)
    forgotten: set[str] = set()
    for _ in range(120):
        roll = rng.random()
        if roll < 0.6:
            text = rng.choice(POOL)
            svc.ingest("u", [C(text, kind=KIND.get(text.split()[0], "preference"),
                               importance=rng.random())], {})
            forgotten.discard(norm(text))
        elif roll < 0.8 and svc.list("u"):
            victim = rng.choice(svc.list("u"))
            svc.forget("u", victim.id)
            if norm(victim.content) not in {norm(i.content) for i in svc.list("u")}:
                forgotten.add(norm(victim.content))
        else:
            ctx = svc.recall("u", rng.choice(POOL))
            shown = {norm(x) for x in ctx.notes()}
            assert not shown & forgotten, "a forgotten note came back"
            st = svc.store.state("u")
            assert not ctx.digest or st.digest_valid
        live = svc.list("u")
        assert len(live) <= 5
        assert len({norm(i.content) for i in live}) == len(live), "duplicate live facts"


# ── API ──────────────────────────────────────────────────────────────────────
def test_me_endpoints_add_edit_preview_and_refuse(client):  # noqa: F811 - the imported fixture
    http, _ = client
    body = http.post("/api/v1/me/memories", json={"content": "use football examples", "kind": "interest"})
    assert body.status_code == 201
    [note] = body.json()["items"]
    assert note["origin"] == "you"
    bad = http.post("/api/v1/me/memories", json={"content": "my mother is a teacher"})
    assert bad.status_code == 422 and "sensitive" in bad.json()["error"]["message"]
    edited = http.patch(f"/api/v1/me/memories/{note['id']}", json={"content": "use cricket examples"})
    assert edited.status_code == 200 and edited.json()["content"] == "use cricket examples"
    assert http.patch(f"/api/v1/me/memories/{note['id']}", json={"content": "again"}).status_code == 404
    preview = http.get("/api/v1/me/memories/recall", params={"q": "explain gravity"}).json()
    assert preview["profile"] == ["use cricket examples"]
    assert "profile" in http.get("/api/v1/me/memories").json()


# ── live calibration (RUN_INTEGRATION=1) ─────────────────────────────────────
@pytest.mark.skipif(not os.environ.get("RUN_INTEGRATION"), reason="needs the real embedder")
def test_the_configured_dense_floor_separates_related_from_unrelated():
    """Swapping the embedder? This fails until ``recall.dense_min`` is recalibrated."""
    from lessonforge.config import MemoryRecallConfig
    from lessonforge.providers.embedding.fastembed import FastEmbedEmbedder
    from lessonforge.services.memory.recall import Snapshot

    emb = FastEmbedEmbedder("BAAI/bge-small-en-v1.5")
    model = "m"
    mem = ["prefers examples about football", "finds fractions hard", "is preparing for the SEE exam",
           "Studied photosynthesis: found light reactions hard"]
    items = [item("u", t, embedding=v, embedding_model=model) for t, v in zip(mem, emb.embed(mem), strict=True)]
    snap = Snapshot.build(1, items, model)
    floor = MemoryRecallConfig().dense_min
    related = {"how does photosynthesis work in leaves?": 3, "adding fractions with unlike denominators": 1,
               "tips for my SEE preparation": 2}
    for q, want in related.items():
        sims = snap.dense(emb.embed_one(q))
        for i, it in enumerate(items):
            assert (sims[it.id] >= floor) == (i == want), (q, it.content, sims[it.id])
    assert max(snap.dense(emb.embed_one("what is newton's second law?")).values()) < floor


def test_an_off_task_or_inventive_digest_is_rejected_for_the_ranked_profile():
    class Rambler(ProfileSynthesizer):
        def synthesize(self, items, *, max_chars):
            return "- Let's think about what you notice?\n- Great question!\n- likes short answers"

    svc = svc_with(synth=Rambler(), profile_refresh_writes=1)
    svc.ingest("u", [C("likes short answers")], {})
    ctx = svc.recall("u")
    assert not ctx.digest and ctx.profile == ["likes short answers"]
