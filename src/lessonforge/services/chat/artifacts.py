"""ArtifactService — resolves a chat artifact to the service that understands its kind.

A quiz is the first artifact kind; flashcards, study plans and worksheets will be
next. Each kind provides a small service (``summary``, ``play_view``, attempts…) and
registers here, so the API and pipeline deal in generic :class:`ChatArtifact`s and
never branch on ``kind`` themselves.
"""

from __future__ import annotations

from typing import Any

from ...activity.store import ActivityRecorder
from ...domain.chat import ArtifactAttempt, ChatArtifact
from .quiz_play import KIND as QUIZ
from .quiz_play import AnswerGrader, QuizSessions
from .store import ChatStore


class ArtifactService:
    def __init__(self, store: ChatStore, grader: AnswerGrader,
                 activity: ActivityRecorder | None = None) -> None:
        self.store = store
        self._kinds: dict[str, QuizSessions] = {QUIZ: QuizSessions(store, grader, activity)}

    def for_kind(self, kind: str) -> QuizSessions:
        try:
            return self._kinds[kind]
        except KeyError:
            raise ValueError(f"unsupported artifact kind {kind!r}") from None

    def summary(self, artifact: ChatArtifact,
                attempts: list[ArtifactAttempt] | None = None) -> dict[str, Any]:
        return self.for_kind(artifact.kind).summary(artifact, attempts)

    def summaries(self, conversation_id: str) -> list[dict[str, Any]]:
        """Every artifact of a conversation with its attempt summary — two queries in
        total (artifacts, then all their attempts at once), whatever the count."""
        artifacts = self.store.list_artifacts(conversation_id)
        if not artifacts:
            return []
        attempts = self.store.attempts_by_artifact(conversation_id)
        out = [self._safe_summary(a, attempts.get(a.id, [])) for a in artifacts]
        return [s for s in out if s is not None]

    def _safe_summary(self, artifact: ChatArtifact,
                      attempts: list[ArtifactAttempt]) -> dict[str, Any] | None:
        try:
            return self.summary(artifact, attempts)
        except Exception:   # a corrupt/legacy artifact must not break reloading the chat
            return None
