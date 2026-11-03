"""MemoryService — the long-term memory facade the chat and learn mode use.

Read path (per turn, cheap): a small **profile slice** (preferences / needs / interests
/ goals) plus the few items most **relevant** to the current question (cosine over the
learner's own embeddings, lexical fallback). Returned as plain notes the prompts label
"about the learner — personalise only".

Write path (always via the job queue, never on the request path): extract → policy →
store for chat turns; and, when a lesson ends, commit **mastery** (code only, with a
forgetting-curve review schedule) plus an **episode** memory.

Every entry point checks the per-user switch and degrades to "no memory" on failure —
memory is an enhancement, never a dependency of answering.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from ...config import LongTermMemoryConfig
from ...domain.learning import LearningSession, kc_key
from ...domain.memory import MasteryRecord, MemoryItem, MemoryOp, PriorTurn
from ...providers.base import Embedder
from ...util import content_terms, overlap
from .extractor import MemoryExtractor
from .jobs import MemoryJobQueue
from .policy import MemoryPolicy
from .store import MemoryStore

log = logging.getLogger("lessonforge.memory")

PROFILE_KINDS = ("preference", "learning_need", "interest", "goal")


def _now() -> datetime:
    return datetime.now(UTC)


def _cos(a: list[float], b: list[float]) -> float:
    num = sum(x * y for x, y in zip(a, b, strict=False))
    den = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return num / den if den else 0.0


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


class MemoryService:
    def __init__(self, *, store: MemoryStore, extractor: MemoryExtractor, policy: MemoryPolicy,
                 jobs: MemoryJobQueue, config: LongTermMemoryConfig,
                 embedder: Embedder | None = None) -> None:
        self.store, self.extractor, self.policy = store, extractor, policy
        self.jobs, self.config, self.embedder = jobs, config, embedder

    # ── read ─────────────────────────────────────────────────────────────────
    def notes_for(self, owner_id: str, query: str | None = None) -> list[str]:
        """Profile slice + the items most relevant to ``query``, as short notes."""
        try:
            if not self.store.enabled(owner_id):
                return []
            items = self.store.live(owner_id)
        except Exception:
            log.exception("memory recall failed")
            return []
        profile = [i for i in items if i.kind in PROFILE_KINDS][: self.config.profile_slice]
        chosen = {i.id: i for i in profile}
        if query and len(items) > len(profile):
            for i in self._relevant(query, [i for i in items if i.id not in chosen]):
                chosen[i.id] = i
        if chosen:
            self.jobs.submit(self.store.touch, owner_id, list(chosen))
        return [i.content for i in chosen.values()]

    def _relevant(self, query: str, items: list[MemoryItem]) -> list[MemoryItem]:
        k = self.config.recall_top_k
        scored: list[tuple[float, MemoryItem]] = []
        qv = None
        if self.embedder is not None and any(i.embedding for i in items):
            try:
                qv = self.embedder.embed([query])[0]
            except Exception:
                qv = None
        for i in items:
            score = (_cos(qv, i.embedding) if qv is not None and i.embedding
                     else max(overlap(query, i.content), overlap(i.content, query)))
            if score >= (0.35 if qv is not None and i.embedding else 0.25):
                scored.append((score, i))
        return [i for _, i in sorted(scored, key=lambda t: t[0], reverse=True)[:k]]

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
                 context: Sequence[PriorTurn] = ()) -> None:
        if not self.store.enabled(owner_id):
            return
        existing = self.store.live(owner_id)
        similar = self._relevant(message, existing) or existing[:5]
        for op in self.extractor.extract(message, similar, context):
            if self.policy.allow(op, existing):
                self._apply(owner_id, op, {"conversation_id": conversation_id, "message_id": message_id})

    def _apply(self, owner_id: str, op: MemoryOp, source: dict) -> None:
        if op.op == "delete" and op.target_id:
            self.store.delete(owner_id, op.target_id)
            return
        item = MemoryItem(owner_id=owner_id, kind=op.kind or "preference", content=op.content.strip(),
                          confidence=op.confidence, source=source, embedding=self._embed(op.content))
        if op.op == "update" and op.target_id:
            self.store.supersede(owner_id, op.target_id, item)
        else:
            self.store.add(item)

    def _embed(self, text: str) -> list[float] | None:
        if self.embedder is None:
            return None
        try:
            return [float(x) for x in self.embedder.embed([text])[0]]
        except Exception:
            return None

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
                   + (f"; found hard: {', '.join(stuck[:2])}" if stuck else ""))
        self.store.add(MemoryItem(
            owner_id=owner, kind="episode", content=summary[:300], confidence=1.0,
            source={"session_id": session.id, "conversation_id": session.conversation_id},
            embedding=self._embed(summary), expires_at=now + timedelta(days=self.config.episode_ttl_days)))

    # ── the learner's own controls ───────────────────────────────────────────
    def list(self, owner_id: str) -> list[MemoryItem]:
        return self.store.live(owner_id)

    def forget(self, owner_id: str, item_id: str) -> bool:
        return self.store.delete(owner_id, item_id)

    def forget_all(self, owner_id: str) -> int:
        return self.store.clear(owner_id)

    def enabled(self, owner_id: str) -> bool:
        return self.store.enabled(owner_id)

    def set_enabled(self, owner_id: str, enabled: bool) -> None:
        self.store.set_enabled(owner_id, enabled)

    def mastery(self, owner_id: str) -> list[MasteryRecord]:
        return sorted(self.store.mastery(owner_id), key=lambda r: r.due_at)
