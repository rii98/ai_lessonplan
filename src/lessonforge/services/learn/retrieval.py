"""Retrieval in learn mode: per objective, not per turn (docs §4).

The drift problem: retrieving on the student's every message lets a tangent pull in
context that takes over the session. So retrieval is a *policy* whose default is
**no**:

- **T0 plan** — the planner retrieves once, broadly, at session start.
- **T1 advance** — an objective's own pins are fetched when it is detailed.
- **T2 coverage gap** — an in-scope question the pinned passages can't answer
  (cheap lexical overlap check) → one anchored search, used for this turn only.
- **T3 remediation** — repeated failure → an alternative explanation from the
  pedagogical / local-context collections, this turn only.

Every query is **anchored** to the active objective, the plan's scope (grade /
subject filters, a locked caller's ``allow_fallback``) is fixed, and turn-only
("ephemeral") chunks never change the session's pinned grounding.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ...config import LearnRetrievalConfig
from ...domain.chat import FilterMode
from ...domain.learning import Decision, LearningSession, MoveKind, PinnedChunk, PlanScope
from ...util import overlap
from ..chat.retrieve import ChatRetriever
from ..chat.transform import TransformedQuery

REMEDIATION_COLLECTIONS = ["pedagogical", "local_context"]


class EvidenceSource(ABC):
    """Where pinned passages come from — the corpus, behind the chat retriever."""

    @abstractmethod
    def search(self, queries: list[str], scope: PlanScope, *, collections: list[str] | None = None,
               top: int = 6) -> list[PinnedChunk]:
        """Ranked passages; ``[]`` on any failure (grounding is never a hard dependency)."""


class ChatEvidenceSource(EvidenceSource):
    def __init__(self, retriever: ChatRetriever) -> None:
        self.retriever = retriever

    def search(self, queries, scope, *, collections=None, top=6):
        queries = [q for q in queries if q.strip()]
        if not queries:
            return []
        cols = collections if collections is not None else scope.collections
        if collections is not None and scope.collections is not None:
            cols = [c for c in collections if c in scope.collections]   # never widen a locked scope
        try:
            chunks = self.retriever.retrieve(
                TransformedQuery(queries=queries, standalone=queries[0]),
                mode=FilterMode.scoped if scope.filters else FilterMode.broad,
                filters=scope.filters, collections=cols, allow_fallback=scope.allow_fallback)
        except Exception:
            return []
        return [PinnedChunk(text=c.text, source=c.source, collection=c.collection,
                            score=c.score, metadata=c.metadata) for c in chunks[:top]]


class NullEvidenceSource(EvidenceSource):
    def search(self, queries, scope, *, collections=None, top=6):
        return []


class RetrievalPolicy(ABC):
    @abstractmethod
    def pins_for(self, topic: str, scope: PlanScope, title: str,
                 ideas: list[str]) -> list[PinnedChunk]:
        """T1: an objective's own pinned passages."""

    @abstractmethod
    def for_turn(self, session: LearningSession, decision: Decision, text: str) -> list[PinnedChunk]:
        """T2/T3: turn-only passages, usually none."""


class AnchoredRetrievalPolicy(RetrievalPolicy):
    def __init__(self, source: EvidenceSource, cfg: LearnRetrievalConfig) -> None:
        self.source = source
        self.cfg = cfg
        self.last_trigger = "none"       # diagnostics for the turn log

    def pins_for(self, topic, scope, title, ideas):
        query = f"{topic}: {title}. " + " ".join(ideas[:4])
        return self.source.search([query.strip()], scope, top=self.cfg.pins_per_objective)

    def for_turn(self, session, decision, text):
        self.last_trigger = "none"
        obj = session.plan.objective(decision.objective_id)
        if decision.retrieve == "coverage_gap" and text.strip():
            pinned = " ".join(p.text for p in obj.evidence)
            if pinned and overlap(text, pinned) >= self.cfg.coverage_threshold:
                return []                # the pins already cover it — no search
            self.last_trigger = "coverage_gap"
            # anchored: the objective is always part of the query (no free drift)
            return self.source.search([f"{session.plan.topic} — {obj.title}: {text[:300]}"],
                                      session.plan.scope, top=3)
        st = session.states[obj.id]
        if decision.move in (MoveKind.reexplain, MoveKind.confront) and st.attempts >= 2:
            self.last_trigger = "remediation"
            mis = next((m.statement for m in obj.misconceptions
                        if m.id in st.misconceptions_seen), "")
            query = f"{obj.title} explained simply with an everyday example. {mis}".strip()
            return self.source.search([query], session.plan.scope,
                                      collections=REMEDIATION_COLLECTIONS, top=2)
        return []


def build_retrieval_policy(cfg: LearnRetrievalConfig, source: EvidenceSource) -> RetrievalPolicy:
    if cfg.provider != "anchored":
        raise ValueError(f"unknown learn retrieval policy {cfg.provider!r}; known: ['anchored']")
    return AnchoredRetrievalPolicy(source, cfg)
