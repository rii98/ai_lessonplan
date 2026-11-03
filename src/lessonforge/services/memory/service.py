"""MemoryService — the long-term memory facade the chat and learn mode use.

**Read path** (per turn, cheap — docs/MEMORY.md §3)::

    state (1 small lookup: switch, version, digest)
      └─ snapshot (cached per version; rows re-read only after a change, in any replica)
           ├─ PROFILE   valid digest (+ notes added since it was built)  |  ranked slice
           └─ RELEVANT  hybrid relevance × recency × importance, diversity, char budget

**Write path** (always on the job queue, never on the request path — §2)::

    extract (fast model: candidate facts from the learner's own words)
      → policy  (minors-first denylist, BEFORE any second model sees it)
      → neighbours (one batched embedding call; similar to each CANDIDATE)
      → reconcile (add / update / retire / no-op; retry → rules fallback)
      → policy  (merged sentences are new text)
      → apply   (one atomic, per-learner-locked unit of work; re-validated)
      → digest refresh when stale · periodic retention + re-embedding

Lessons commit **mastery** (code only, with a forgetting-curve review schedule) plus an
**episode** memory. Every entry point checks the per-user switch and degrades to "no
memory" on failure — memory is an enhancement, never a dependency of answering.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from ...config import LongTermMemoryConfig
from ...domain.learning import LearningSession, kc_key
from ...domain.memory import (
    ApplyResult,
    Candidate,
    MasteryRecord,
    MemoryContext,
    MemoryItem,
    MemoryKind,
    MemoryOp,
    MemoryState,
    PriorTurn,
)
from ...providers.base import Embedder
from ...util import content_terms
from .extractor import MemoryExtractor
from .jobs import MemoryJobQueue
from .policy import MemoryPolicy
from .profile import NoSynthesizer, ProfileSynthesizer, digest_lines, ranked_profile
from .recall import (
    Scoring,
    Snapshot,
    SnapshotCache,
    embedder_id,
    lexical,
    rank,
    relevance,
    within_budget,
)
from .reconciler import MemoryReconciler, RulesReconciler
from .store import Change, MemoryStore

log = logging.getLogger("lessonforge.memory")


def _now() -> datetime:
    return datetime.now(UTC)


_FRAME = {"what", "whats", "why", "how", "when", "where", "which", "who", "explain", "teach",
          "about", "learn", "tell", "describe", "define", "meaning"}


def _slug_terms(slug: str) -> set[str]:
    return {t for t in content_terms(slug.replace("-", " ")) if t not in _FRAME}


def _jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a | b else 0.0


def same_concept(a: str, b: str) -> float:
    """Similarity of two free-text knowledge-component keys (0..1): the weaker of the
    topic similarity and the objective-title similarity, so both must agree. Question
    framing ("what is", "teach me about") carries no meaning and is ignored."""
    try:
        ta, oa = a.removeprefix("free:").split("/", 1)
        tb, ob = b.removeprefix("free:").split("/", 1)
    except ValueError:
        return 0.0
    return min(_jaccard(_slug_terms(ta), _slug_terms(tb)), _jaccard(_slug_terms(oa), _slug_terms(ob)))


_GROUNDED = 0.34      # a digest line must share ≥ 1/3 of its meaning words with some memory


class MemoryRefused(ValueError):
    """A note the learner typed that policy will not keep (the message says why)."""


class MemoryService:
    def __init__(self, *, store: MemoryStore, extractor: MemoryExtractor, policy: MemoryPolicy,
                 jobs: MemoryJobQueue, config: LongTermMemoryConfig,
                 embedder: Embedder | None = None, reconciler: MemoryReconciler | None = None,
                 synthesizer: ProfileSynthesizer | None = None) -> None:
        self.store, self.extractor, self.policy = store, extractor, policy
        self.jobs, self.config, self.embedder = jobs, config, embedder
        self.reconciler = reconciler or RulesReconciler(duplicate_similarity=config.duplicate_similarity)
        self.synthesizer = synthesizer or NoSynthesizer()
        r = config.recall
        self.scoring = Scoring(dense_min=r.dense_min, lexical_min=r.lexical_min,
                               w_relevance=r.w_relevance, w_recency=r.w_recency,
                               w_importance=r.w_importance, half_life_days=r.half_life_days,
                               diversity=r.diversity)
        self.cache = SnapshotCache(config.cache_owners)
        self._model: str | None = None
        self._model_known = False
        self._maint_lock = threading.Lock()
        self._last_maintenance = 0.0

    # ── embeddings ───────────────────────────────────────────────────────────
    @property
    def model(self) -> str | None:
        if not self._model_known:
            self._model, self._model_known = embedder_id(self.embedder), True
        return self._model

    def _embed(self, texts: list[str]) -> list[list[float] | None]:
        """One batched call; ``None`` per text on failure (lexical still works)."""
        if self.embedder is None or not texts:
            return [None] * len(texts)
        try:
            return [[float(x) for x in v] for v in self.embedder.embed(list(texts))]
        except Exception:
            log.warning("memory embedding failed; lexical only", exc_info=True)
            return [None] * len(texts)

    # ── snapshot ─────────────────────────────────────────────────────────────
    def _snapshot(self, owner_id: str, state: MemoryState) -> Snapshot:
        snap = self.cache.get(owner_id, state.version, self.model)
        if snap is None:
            snap = Snapshot.build(state.version, self.store.live(owner_id), self.model)
            self.cache.put(owner_id, snap)
        return snap

    def _neighbours(self, text_vecs: list[tuple[str, list[float] | None]], snap: Snapshot,
                    items: list[MemoryItem]) -> list[list[tuple[MemoryItem, float]]]:
        out = []
        for text, vec in text_vecs:
            rel = relevance(text, items, snap.dense(vec), self.scoring)
            hits = sorted(((i, rel[i.id]) for i in items if rel.get(i.id, 0) >= self.config.neighbour_min),
                          key=lambda t: t[1], reverse=True)[: self.config.neighbours_k]
            out.append(hits)
        return out

    # ── read ─────────────────────────────────────────────────────────────────
    def recall(self, owner_id: str, query: str | None = None) -> MemoryContext:
        """Profile + relevant notes for one turn. Never raises."""
        try:
            state = self.store.state(owner_id)
            if not state.enabled:
                return MemoryContext()
            snap = self._snapshot(owner_id, state)
            ctx = self._compose(snap, state, query)
        except Exception:
            log.exception("memory recall failed")
            return MemoryContext()
        if ctx.ids:
            self.jobs.submit(self.store.touch, owner_id, list(ctx.ids))
        return ctx

    def _compose(self, snap: Snapshot, state: MemoryState, query: str | None) -> MemoryContext:
        now = _now()
        items = [i for i in snap.items if i.is_live(now)]
        cfg = self.config
        shown: set[str] = set()
        digest = bool(state.digest_valid and not isinstance(self.synthesizer, NoSynthesizer))
        if digest:
            built = state.profile.built_at
            profile = digest_lines(state.profile.content)
            fresh = [i for i in items if i.kind in cfg.profile_kinds and i.created_at > built]
            for i in ranked_profile(fresh, kinds=cfg.profile_kinds, max_lines=cfg.profile_slice, now=now):
                profile.append(i.content)
                shown.add(i.id)
            pool = [i for i in items if i.kind in cfg.situational_kinds and i.id not in shown]
        else:
            chosen = ranked_profile(items, kinds=cfg.profile_kinds, max_lines=cfg.profile_slice, now=now,
                                    half_life_days=self.scoring.half_life_days)
            profile = [i.content for i in chosen]
            shown = {i.id for i in chosen}
            pool = [i for i in items if i.id not in shown]
        relevant: list[MemoryItem] = []
        if query and pool:
            [qvec] = self._embed([query]) if snap.has_vectors else [None]
            rel = relevance(query, pool, snap.dense(qvec), self.scoring)
            # the profile already says it (a digest paraphrases): don't repeat it
            pool = [i for i in pool if max((lexical(i.content, p) for p in profile), default=0.0) < 0.8]
            relevant = rank(pool, rel, snap, self.scoring, k=cfg.top_k, now=now)
        budget = cfg.recall.budget_chars
        profile = within_budget(profile, budget)
        rest = budget - sum(len(p) + 1 for p in profile)
        rel_lines = within_budget([i.content for i in relevant], max(0, rest))
        used = shown | {i.id for i in relevant[: len(rel_lines)]}
        return MemoryContext(profile=profile, relevant=rel_lines, ids=sorted(used), digest=digest)

    def notes_for(self, owner_id: str, query: str | None = None) -> list[str]:
        """Profile + relevant notes as one flat list (learn mode's shape)."""
        return self.recall(owner_id, query).notes()

    def prior(self, owner_id: str, kc_refs: list[str]) -> dict[str, float]:
        """Forgetting-discounted mastery to seed a new lesson, keyed by the REQUESTED key
        (a free-text key is resolved to the record of the same concept, see :meth:`resolve`)."""
        try:
            if not self.store.enabled(owner_id):
                return {}
            resolved = self.resolve(owner_id, kc_refs)
            records = {r.kc_ref: r for r in self.store.mastery(owner_id, list(set(resolved.values())))}
            return {k: records[v].current() for k, v in resolved.items() if v in records}
        except Exception:
            log.exception("mastery prior failed")
            return {}

    def resolve(self, owner_id: str, keys: list[str]) -> dict[str, str]:
        """Entity resolution for knowledge components: requested key → stored key.

        A curriculum code is an identity. A free-text key ("free:<topic>/<objective>") is
        a *description*, and the same concept arrives worded differently every lesson
        ("What is ammonia?" / "what's ammonia" / "ammonia"), so exact string equality
        silently forks the learner's history. An unknown free key is matched to the
        learner's existing record whose topic AND objective title mean the same (content-
        term similarity over both, threshold in config); otherwise it stays new."""
        out = {k: k for k in keys}
        known = {r.kc_ref for r in self.store.mastery(owner_id, keys)}
        missing = [k for k in keys if k not in known and k.startswith("free:")]
        if not missing:
            return out
        pool = [r.kc_ref for r in self.store.mastery(owner_id) if r.kc_ref.startswith("free:")]
        taken = set(known)
        for k in missing:
            scored = sorted(((same_concept(k, ref), ref) for ref in pool if ref not in taken),
                            reverse=True)
            if scored and scored[0][0] >= self.config.concept_match:
                out[k] = scored[0][1]
                taken.add(scored[0][1])
        return out

    # ── write (queued) ───────────────────────────────────────────────────────
    def remember_turn(self, owner_id: str, conversation_id: str, message: str,
                      message_id: str | None = None, context: Sequence[PriorTurn] = ()) -> None:
        """Queue extraction. ``context`` = the messages just before ``message`` (so "the
        above style" / "yes" can be understood) — context only, never a source."""
        self.jobs.submit(self._extract, owner_id, conversation_id, message, message_id,
                         list(context))

    def commit_session(self, session: LearningSession) -> None:
        self.jobs.submit(self._commit, session.model_copy(deep=True))

    def _extract(self, owner_id: str, conversation_id: str, message: str, message_id: str | None,
                 context: Sequence[PriorTurn] = ()) -> ApplyResult:
        if not self.store.enabled(owner_id):
            return ApplyResult()
        candidates = self.extractor.extract(message, context)
        return self.ingest(owner_id, candidates,
                           {"conversation_id": conversation_id, "message_id": message_id})

    def ingest(self, owner_id: str, candidates: list[Candidate], source: dict) -> ApplyResult:
        """The write pipeline for candidate facts (see module doc). Synchronous — callers
        on the request path go through :meth:`remember_turn` (queued)."""
        candidates = [c for c in candidates if self.policy.candidate_reason(c) is None]
        if not candidates:
            return ApplyResult()
        state = self.store.state(owner_id)
        if not state.enabled:
            return ApplyResult()
        snap = self._snapshot(owner_id, state)
        live = [i for i in snap.items if i.is_live()]
        vecs = self._embed([c.content for c in candidates])
        neighbours = self._neighbours([(c.content, v) for c, v in zip(candidates, vecs, strict=True)],
                                      snap, live)
        ops = [op for op in self.reconciler.reconcile(candidates, neighbours) if self.policy.allow(op, live)]
        result = self._apply(owner_id, ops, source, known={c.content: v for c, v in zip(candidates, vecs, strict=True)})
        if result.changed:
            self._after_write(owner_id)
        return result

    def _apply(self, owner_id: str, ops: list[MemoryOp], source: dict,
               known: dict[str, list[float] | None] | None = None) -> ApplyResult:
        """Build rows for the approved ops (embedding new text in one call) and apply
        them atomically."""
        known = dict(known or {})
        texts = [op.content for op in ops if op.op != "retire" and op.content not in known]
        for t, v in zip(texts, self._embed(texts), strict=True):
            known[t] = v
        changes = []
        for op in ops:
            if op.op == "retire":
                changes.append(Change("retire", target_id=op.target_id))
                continue
            vec = known.get(op.content)
            item = MemoryItem(owner_id=owner_id, kind=op.kind or "preference", content=op.content.strip(),
                              confidence=op.confidence, importance=op.importance,
                              source={**source, **({"explicit": True} if op.explicit else {})},
                              embedding=vec, embedding_model=self.model if vec is not None else None)
            changes.append(Change(op.op, item=item, target_id=op.target_id))
        result = self.store.apply(owner_id, changes, max_items=self.config.max_items_per_user)
        if result.changed:
            log.info("memory.apply owner=%s add=%d update=%d retire=%d evict=%d skip=%d", owner_id,
                     len(result.added), len(result.updated), len(result.retired), len(result.evicted),
                     result.skipped)
        return result

    def _after_write(self, owner_id: str) -> None:
        """Upkeep that follows a change: the digest (if stale) and periodic maintenance.
        Called from inside a job, so it is already off the request path."""
        try:
            self.refresh_profile(owner_id)
        except Exception:
            log.exception("memory profile refresh failed")
        self._maybe_maintain()

    def _commit(self, session: LearningSession) -> None:
        """Mastery per objective (code only) + one episode memory for the lesson."""
        owner = session.owner_id
        if not self.store.enabled(owner):
            return
        now = _now()
        raw = {o.id: kc_key(o, session.plan.topic) for o in session.plan.objectives}
        resolved = self.resolve(owner, list(raw.values()))
        keys = {oid: resolved[k] for oid, k in raw.items()}
        known = {r.kc_ref: r for r in self.store.mastery(owner, list(keys.values()))}
        for o in session.plan.objectives:
            st = session.states[o.id]
            if st.status == "pending" and not st.early_evidence:
                continue                         # never reached: nothing learned about it
            rec = known.get(keys[o.id]) or MasteryRecord(owner_id=owner, kc_ref=keys[o.id],
                                                         title=o.title, p_mastery=st.p_mastery)
            mastered = st.status in ("mastered", "skipped")
            # FSRS-lite: a successful review multiplies stability; a lapse resets it
            rec.stability = max(1.0, rec.stability * 2.5) if mastered else 1.0
            rec.p_mastery = round(st.p_mastery, 3)
            rec.attempts += st.checks_passed + st.attempts
            rec.correct += st.checks_passed
            rec.title = o.title
            rec.last_seen = now
            rec.due_at = now + timedelta(days=rec.stability)
            self.store.upsert_mastery(rec)
        states = [session.states[o.id] for o in session.plan.objectives]
        mastered = sum(st.status == "mastered" for st in states)
        covered = sum(st.status in ("mastered", "skipped", "left") for st in states)
        stuck = [o.title for o in session.plan.objectives
                 if session.states[o.id].misconceptions_seen or session.states[o.id].attempts >= 2]
        # the note must agree with the progress bars: "mastered" only for what the
        # learner model actually counts as mastered; the rest was worked through
        summary = (f"Studied {session.plan.topic} on {now:%d %b}: worked through {covered}/"
                   f"{len(states)} parts, mastered {mastered}"
                   + (f"; found hard: {', '.join(stuck[:2])}" if stuck else ""))[:300]
        [vec] = self._embed([summary])
        episode = MemoryItem(owner_id=owner, kind="episode", content=summary, confidence=1.0,
                             importance=0.7 if stuck else 0.5,
                             source={"session_id": session.id, "conversation_id": session.conversation_id},
                             embedding=vec, embedding_model=self.model if vec is not None else None,
                             expires_at=now + timedelta(days=self.config.episode_ttl_days))
        if self.store.apply(owner, [Change("add", item=episode)],
                            max_items=self.config.max_items_per_user).changed:
            self._after_write(owner)

    # ── the learner's own controls (synchronous) ─────────────────────────────
    def remember(self, owner_id: str, content: str, kind: MemoryKind = "preference") -> list[MemoryItem]:
        """A note the learner adds themselves ("remember that …"). Reconciled like any
        other fact (so it updates rather than duplicates); refused content raises
        :class:`MemoryRefused` with the reason."""
        cand = Candidate(kind=kind, content=" ".join(content.split()), confidence=1.0, importance=1.0,
                         explicit=True)
        why = self.policy.candidate_reason(cand)
        if why:
            raise MemoryRefused(why)
        if not self.store.enabled(owner_id):
            raise MemoryRefused("memory is switched off")
        result = self.ingest(owner_id, [cand], {"explicit": True})
        if not result.changed:
            raise MemoryRefused("already remembered")
        return result.written()

    def edit(self, owner_id: str, item_id: str, content: str) -> MemoryItem:
        """The learner rewrites one of their notes (kept as an explicit note)."""
        live = self.store.live(owner_id)
        target = next((i for i in live if i.id == item_id), None)
        if target is None:
            raise KeyError(item_id)
        op = MemoryOp(op="update", target_id=item_id, kind=target.kind, content=" ".join(content.split()),
                      confidence=1.0, importance=max(target.importance, 0.9), explicit=True)
        why = self.policy.reason(op, live)
        if why:
            raise MemoryRefused(why)
        result = self._apply(owner_id, [op], {"explicit": True})
        if not result.updated:
            raise MemoryRefused("nothing changed")
        self._after_write(owner_id)
        return result.updated[0]

    def list(self, owner_id: str) -> list[MemoryItem]:
        return self.store.live(owner_id)

    def forget(self, owner_id: str, item_id: str) -> bool:
        ok = self.store.delete(owner_id, item_id)
        if ok:   # the digest may mention it: it is hidden at once, and rebuilt without it
            self.jobs.submit(self._safe_refresh, owner_id)
        return ok

    def forget_all(self, owner_id: str) -> int:
        n = self.store.clear(owner_id)
        self.cache.drop(owner_id)
        return n

    def enabled(self, owner_id: str) -> bool:
        return self.store.enabled(owner_id)

    def set_enabled(self, owner_id: str, enabled: bool) -> None:
        self.store.set_enabled(owner_id, enabled)

    def mastery(self, owner_id: str) -> list[MasteryRecord]:
        return sorted(self.store.mastery(owner_id), key=lambda r: r.due_at)

    def profile_text(self, owner_id: str) -> str | None:
        """The digest the learner's lessons currently see, if one is valid."""
        state = self.store.state(owner_id)
        return state.profile.content if state.digest_valid and state.profile else None

    # ── the digest ───────────────────────────────────────────────────────────
    def _safe_refresh(self, owner_id: str) -> None:
        try:
            self.refresh_profile(owner_id)
        except Exception:
            log.exception("memory profile refresh failed")

    def refresh_profile(self, owner_id: str, *, force: bool = False) -> bool:
        """Rebuild the digest when stale (or ``force``). Returns whether one was saved."""
        if isinstance(self.synthesizer, NoSynthesizer):
            return False
        state = self.store.state(owner_id)
        if not state.enabled:
            return False
        if not force and state.digest_valid and state.profile is not None:
            p = state.profile
            built = p.built_at if p.built_at.tzinfo else p.built_at.replace(tzinfo=UTC)
            fresh_enough = (_now() - built) < timedelta(days=self.config.profile_max_age_days)
            if fresh_enough and state.version - p.built_version < self.config.profile_refresh_writes:
                return False
        items = [i for i in self._snapshot(owner_id, state).items if i.is_live()]
        if not items:
            return False
        items.sort(key=lambda i: (-i.importance, -i.created_at.timestamp()))
        text = self.synthesizer.synthesize(items[:80], max_chars=self.config.profile_max_chars)
        if not text:
            return False
        bullets = digest_lines(text)
        # validate before storing: every line must be policy-clean AND grounded in some
        # memory (a model that went off-task, or invented a fact, produces lines that
        # share no meaning words with any source); mostly-ungrounded → reject it all
        grounded = [b for b in bullets if self.policy.allow_line(b)
                    and max((lexical(b, i.content) for i in items[:80]), default=0.0) >= _GROUNDED]
        if not grounded or len(grounded) * 2 < len(bullets):
            log.warning("memory digest rejected owner=%s (%d/%d lines grounded)", owner_id,
                        len(grounded), len(bullets))
            return False
        lines = [f"- {b}" for b in grounded]
        # built against ``state.version``: a removal that lands while synthesising bumps
        # removal_version past it, so this digest is never shown (correct by construction)
        self.store.save_profile(owner_id, "\n".join(lines), state.version)
        return True

    # ── maintenance ──────────────────────────────────────────────────────────
    def _maybe_maintain(self) -> None:
        interval = self.config.maintenance_interval_hours * 3600
        if interval <= 0 or not self._maint_lock.acquire(blocking=False):
            return
        try:
            if time.monotonic() - self._last_maintenance < interval and self._last_maintenance:
                return
            self._last_maintenance = time.monotonic()
        finally:
            self._maint_lock.release()
        try:
            self.maintain()
        except Exception:
            log.exception("memory maintenance failed")

    def maintain(self, *, now: datetime | None = None, reembed_limit: int = 200) -> dict[str, int]:
        """Idempotent hygiene, safe at any frequency: prune history and expired rows past
        retention; re-embed live items whose vector is missing or from another embedder
        (so swapping the embedding model in config heals itself)."""
        pruned = self.store.prune(retention_days=self.config.retention_days, now=now)
        reembedded = 0
        if self.model is not None:
            stale = self.store.stale_embeddings(self.model, limit=reembed_limit)
            for item, vec in zip(stale, self._embed([i.content for i in stale]), strict=True):
                if vec is not None and not any(math.isnan(x) for x in vec[:4]):
                    self.store.set_embedding(item.owner_id, item.id, vec, self.model)
                    reembedded += 1
        if pruned or reembedded:
            log.info("memory.maintenance pruned=%d reembedded=%d", pruned, reembedded)
        return {"pruned": pruned, "reembedded": reembedded}
