"""ArtifactService — resolves a chat artifact to the service that understands its kind.

A quiz is the first artifact kind; flashcards, study plans and worksheets will be
next. Each kind provides a small service (``summary``, ``play_view``, attempts…) and
registers here, so the API and pipeline deal in generic :class:`ChatArtifact`s and
never branch on ``kind`` themselves.
"""

from __future__ import annotations

from typing import Any

from ...domain.chat import ChatArtifact
from .quiz_play import KIND as QUIZ
from .quiz_play import AnswerGrader, QuizSessions
from .store import ChatStore


class ArtifactService:
    def __init__(self, store: ChatStore, grader: AnswerGrader) -> None:
        self.store = store
        self._kinds: dict[str, QuizSessions] = {QUIZ: QuizSessions(store, grader)}

    def for_kind(self, kind: str) -> QuizSessions:
        try:
            return self._kinds[kind]
        except KeyError:
            raise ValueError(f"unsupported artifact kind {kind!r}") from None

    def summary(self, artifact: ChatArtifact) -> dict[str, Any]:
        return self.for_kind(artifact.kind).summary(artifact)

    def summaries(self, conversation_id: str) -> list[dict[str, Any]]:
        out = [self._safe_summary(a) for a in self.store.list_artifacts(conversation_id)]
        return [s for s in out if s is not None]

    def _safe_summary(self, artifact: ChatArtifact) -> dict[str, Any] | None:
        try:
            return self.summary(artifact)
        except Exception:   # a corrupt/legacy artifact must not break reloading the chat
            return None
