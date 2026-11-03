"""Learn mode — scaffolded tutoring sessions inside the chat (docs/LEARN_MODE.md).

``build_learn_service`` wires every port from :class:`LearnConfig`; nothing else in
the system names a concrete tracer, policy or renderer.
"""

from __future__ import annotations

from collections.abc import Callable

from ...config import LearnConfig
from ...db.pool import Database
from ...domain.learning import LearningSession
from ...providers.base import LLMClient
from ..chat.quiz_play import AnswerGrader
from ..chat.retrieve import ChatRetriever
from .guard import build_guard
from .learner import build_learner
from .planner import PlanningError, build_planner
from .policy import build_policy
from .renderer import build_renderer
from .retrieval import (
    ChatEvidenceSource,
    EvidenceSource,
    NullEvidenceSource,
    build_retrieval_policy,
)
from .service import LearnInputError, LearnService, LearnTurn, scope_from
from .store import LearningSessionStore, SessionConflict, build_session_store
from .tracer import build_tracer
from .widgets import WidgetService

__all__ = ["LearnInputError", "LearnService", "LearnTurn", "PlanningError", "SessionConflict",
           "build_learn_service", "scope_from"]


def build_learn_service(
    cfg: LearnConfig, *, llm: LLMClient, llm_fast: LLMClient, grader: AnswerGrader,
    retriever: ChatRetriever | None = None, source: EvidenceSource | None = None,
    store: LearningSessionStore | None = None, db: Database | None = None,
    prior: Callable[[str, list[str]], dict[str, float]] | None = None,
    notes: Callable[[str], list[str]] | None = None,
    on_end: Callable[[LearningSession], None] | None = None,
) -> LearnService:
    source = source or (ChatEvidenceSource(retriever) if retriever is not None else NullEvidenceSource())
    retrieval = build_retrieval_policy(cfg.retrieval, source)
    return LearnService(
        store=store or build_session_store(cfg.store, db=db),
        planner=build_planner(cfg.planner, llm=llm, source=source, retrieval=retrieval),
        tracer=build_tracer(cfg.tracer, llm=llm_fast),
        learner=build_learner(cfg.learner),
        policy=build_policy(cfg.policy),
        retrieval=retrieval,
        renderer=build_renderer(cfg.renderer, llm=llm, temperature=cfg.temperature),
        guard=build_guard(cfg.guard),
        widgets=WidgetService(grader),
        config=cfg, prior=prior, notes=notes, on_end=on_end,
    )
