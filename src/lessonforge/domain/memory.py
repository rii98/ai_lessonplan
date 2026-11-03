"""Long-term user memory — the L3 layer (docs/MEMORY.md).

Two kinds of record with deliberately different write rules:

- :class:`MemoryItem` — short statements *about the learner* (a preference, a learning
  need, an interest, a goal, or an episode summary of a finished lesson). Proposed by the
  fast model as :class:`Candidate` facts, filtered by a policy (allowlist + PII denylist —
  these are minors), reconciled against what is already known (:class:`MemoryOp`), never
  facts about the subject, never cited, never read by an access decision. Replaced, not
  erased: an update *supersedes* the old row and a contradiction *retires* it (both set
  ``ended_at``), so history survives until retention prunes it. Only the learner's own
  "forget" hard-deletes.
- :class:`MasteryRecord` — per learner × knowledge component, written ONLY by code
  (the learner model / quiz grading), with a forgetting-curve review schedule.

Read side: :class:`MemoryState` (switch + generation counters + the stored digest, one
cheap query) and :class:`MemoryContext` (what a turn is given).
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

MemoryKind = Literal["preference", "learning_need", "interest", "goal", "episode"]
MEMORY_KINDS: tuple[str, ...] = ("preference", "learning_need", "interest", "goal", "episode")
EndReason = Literal["superseded", "retired", "evicted"]


def _now() -> datetime:
    return datetime.now(UTC)


class MemoryItem(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    owner_id: str
    kind: MemoryKind
    content: str = Field(min_length=3, max_length=300)
    embedding: list[float] | None = None
    embedding_model: str | None = None   # which embedder made ``embedding`` (vectors from
                                         # different models are not comparable)
    source: dict[str, Any] = Field(default_factory=dict)   # provenance: conversation / message / session
    confidence: float = Field(default=0.7, ge=0, le=1)     # how sure we are it is TRUE
    importance: float = Field(default=0.5, ge=0, le=1)     # how costly forgetting it would be
    valid_from: datetime = Field(default_factory=_now)
    superseded_by: str | None = None
    ended_at: datetime | None = None     # superseded / retired / evicted at — no longer live
    end_reason: EndReason | None = None
    last_used_at: datetime | None = None
    expires_at: datetime | None = None
    created_at: datetime = Field(default_factory=_now)

    def is_live(self, now: datetime | None = None) -> bool:
        return self.ended_at is None and self.superseded_by is None and (
            self.expires_at is None or self.expires_at > (now or _now()))

    @property
    def origin(self) -> str:
        """Where it came from, in the learner's terms."""
        if self.source.get("explicit"):
            return "you"
        return "lesson" if self.source.get("session_id") else "chat"

    def public(self) -> dict[str, Any]:
        """What "What LessonForge remembers about me" shows."""
        return {"id": self.id, "kind": self.kind, "content": self.content,
                "importance": self.importance, "origin": self.origin,
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


class PriorTurn(BaseModel):
    """One message BEFORE the student's new one, given to the extractor so references
    ("the above style", "yes, that one") can be understood. Context only — a memory
    may record only what the STUDENT states or agrees to."""

    role: Literal["user", "assistant"]
    text: str


class Candidate(BaseModel):
    """A memory-worthy fact proposed by extraction, BEFORE it is compared with what is
    already known. Extraction answers "is this worth remembering?"; reconciliation
    answers "how does it change the store?" — two judgements, two stages."""

    kind: MemoryKind
    content: str
    confidence: float = Field(default=0.7, ge=0, le=1)
    importance: float = Field(default=0.5, ge=0, le=1)
    explicit: bool = False           # the learner asked: "remember that …", "from now on …"


class MemoryOp(BaseModel):
    """One change to the store: the reconciler's decision for a candidate (or a direct
    edit). The policy may veto it; the store applies a batch atomically.

    - ``add``     a new item
    - ``update``  supersede ``target_id`` with ``content`` (the merged, current fact)
    - ``retire``  ``target_id`` is now wrong (contradicted / resolved) — soft, kept as history
    """

    op: Literal["add", "update", "retire"]
    kind: MemoryKind | None = None
    content: str = ""
    target_id: str | None = None
    confidence: float = 0.7
    importance: float = 0.5
    explicit: bool = False


@dataclass(slots=True)
class StoredProfile:
    """The synthesised digest of a learner's memories, and the store version it saw."""

    content: str
    built_version: int
    built_at: datetime


@dataclass(slots=True)
class MemoryState:
    """Everything a read needs to decide what to do, in one cheap lookup.

    ``version`` increases on every change to the learner's items (it keys the recall
    cache, across processes). ``removal_version`` is the version of the last change that
    REMOVED or REWROTE something (forget, retire, supersede, evict): a digest built
    before it may mention something that is no longer true — or that the learner asked
    us to forget — so it must not be shown."""

    enabled: bool = True
    version: int = 0
    removal_version: int = 0
    profile: StoredProfile | None = None

    @property
    def digest_valid(self) -> bool:
        return self.profile is not None and self.profile.built_version >= self.removal_version


@dataclass(slots=True)
class ApplyResult:
    added: list[MemoryItem] = field(default_factory=list)
    updated: list[MemoryItem] = field(default_factory=list)
    retired: list[str] = field(default_factory=list)
    evicted: list[str] = field(default_factory=list)
    skipped: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.added or self.updated or self.retired or self.evicted)

    def written(self) -> list[MemoryItem]:
        return [*self.added, *self.updated]


@dataclass(slots=True)
class MemoryContext:
    """What one turn is given: an always-on PROFILE (who the learner is — the digest, or
    a ranked slice when no valid digest exists) plus the few items RELEVANT to the
    current question. Personalises only; never a source, never cited."""

    profile: list[str] = field(default_factory=list)
    relevant: list[str] = field(default_factory=list)
    ids: list[str] = field(default_factory=list)
    digest: bool = False             # the profile came from the synthesised digest

    @property
    def is_empty(self) -> bool:
        return not self.profile and not self.relevant

    def notes(self) -> list[str]:
        """Flat list (profile first, de-duplicated) — the shape learn mode consumes."""
        seen: set[str] = set()
        out = []
        for line in (*self.profile, *self.relevant):
            key = line.casefold()
            if key not in seen:
                seen.add(key)
                out.append(line)
        return out
