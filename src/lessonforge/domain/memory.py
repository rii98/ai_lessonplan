"""Long-term user memory — the L3 layer (docs/LEARN_MODE.md §7).

Two kinds of record with deliberately different write rules:

- :class:`MemoryItem` — short statements *about the learner* (a preference, a learning
  need, an interest, a goal, or an episode summary of a finished lesson). Extracted by
  the fast model, filtered by a policy (allowlist + PII denylist — these are minors),
  never facts about the subject, never cited, never read by an access decision.
  Replaced, not erased (``superseded_by``), so "prefers Nepali → now prefers English"
  keeps its history until retention prunes it.
- :class:`MasteryRecord` — per learner × knowledge component, written ONLY by code
  (the learner model / quiz grading), with a forgetting-curve review schedule.
"""

from __future__ import annotations

import math
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

MemoryKind = Literal["preference", "learning_need", "interest", "goal", "episode"]
MEMORY_KINDS: tuple[str, ...] = ("preference", "learning_need", "interest", "goal", "episode")


def _now() -> datetime:
    return datetime.now(UTC)


class MemoryItem(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    owner_id: str
    kind: MemoryKind
    content: str = Field(min_length=3, max_length=300)
    embedding: list[float] | None = None
    source: dict[str, Any] = Field(default_factory=dict)   # conversation / message / session ids
    confidence: float = Field(default=0.7, ge=0, le=1)
    valid_from: datetime = Field(default_factory=_now)
    superseded_by: str | None = None
    last_used_at: datetime | None = None
    expires_at: datetime | None = None
    created_at: datetime = Field(default_factory=_now)

    def public(self) -> dict[str, Any]:
        """What "What LessonForge remembers about me" shows."""
        return {"id": self.id, "kind": self.kind, "content": self.content,
                "created_at": self.created_at, "last_used_at": self.last_used_at,
                "expires_at": self.expires_at}


class MasteryRecord(BaseModel):
    owner_id: str
    kc_ref: str
    title: str = ""
    p_mastery: float = Field(ge=0, le=1)
    attempts: int = 0
    correct: int = 0
    stability: float = 1.0          # days until recall probability drops to 90%
    last_seen: datetime = Field(default_factory=_now)
    due_at: datetime = Field(default_factory=_now)

    def retrievability(self, now: datetime | None = None) -> float:
        """FSRS-style recall probability: 0.9 when ``elapsed == stability``."""
        days = max(0.0, ((now or _now()) - self.last_seen).total_seconds() / 86400)
        return math.exp(math.log(0.9) * days / max(self.stability, 0.1))

    def current(self, now: datetime | None = None) -> float:
        """Mastery discounted by forgetting — what a new session should assume."""
        return round(self.p_mastery * self.retrievability(now), 3)

    def public(self) -> dict[str, Any]:
        return {"kc_ref": self.kc_ref, "title": self.title, "mastery": self.current(),
                "attempts": self.attempts, "correct": self.correct,
                "last_seen": self.last_seen, "due_at": self.due_at,
                "due": self.due_at <= _now()}


class MemoryOp(BaseModel):
    """One change the extractor proposes; the policy may veto it."""

    op: Literal["add", "update", "delete"]
    kind: MemoryKind | None = None
    content: str = ""
    target_id: str | None = None    # update / delete
    confidence: float = 0.7
