"""MemoryExtractor — decide what in the learner's new message is worth remembering.

Stage 1 of the write path (extract → policy → **reconcile** → apply): this stage only
answers "is there a durable fact here?" and emits :class:`Candidate` facts with a kind,
confidence and importance. How a candidate changes the store (new / refines / contradicts
/ redundant) is the reconciler's job, done against the memories most similar to the
CANDIDATE — not to the raw message — so neighbourhoods are precise.

Only the learner's OWN words are a source (never the assistant's reply, which would let
the model "remember" what it said); the previous exchange is context for resolving
references. Conservative by instruction: durable facts about how this learner learns —
not what they asked about (that's in the transcript).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence

from ...domain.memory import MEMORY_KINDS, Candidate, PriorTurn
from ...providers.base import LLMClient
from ...util import extract_json

_SYSTEM = (
    "You maintain a small memory about a school student to personalise tutoring. Read the "
    "student's NEW message and decide if it reveals a DURABLE fact about how they learn: a "
    "preference (language, examples, pace, format), a learning_need (something they find "
    "hard), an interest (a hobby to use in examples), or a goal (an exam, a target). "
    "Ignore questions about the subject, one-off requests, and greetings. NEVER record "
    "names, contact details, family, health, religion, caste, money, marks, or anything "
    "about other people.\n"
    "Output ONLY JSON: {\"facts\": [{\"kind\": \"preference|learning_need|interest|goal\", "
    "\"content\": \"<one short third-person statement>\", \"confidence\": 0..1, "
    "\"importance\": 0..1, \"explicit\": true|false}]}\n"
    "- importance = how much a future lesson would suffer from not knowing it.\n"
    "- explicit = the student directly asked you to remember it (\"remember that…\", \"from now "
    "on…\", \"always/never…\"); those get importance 1.0.\n"
    "- If the student withdraws or reverses something (\"stop using football examples\", "
    "\"actually, English is fine\"), state the NEW fact (\"does not want football examples\"). "
    "If something they found hard is now easy for them (\"I get fractions now\"), say so "
    "(\"no longer finds fractions hard\") — that is a durable change too.\n"
    "The RECENT CONVERSATION is context only: use it to understand what the student's new "
    "message refers to (\"the above style\", \"yes, that one\", \"like this\"). Record ONLY what "
    "the STUDENT states or agrees to — never something the tutor said, offered or assumed "
    "that the student did not accept. Write every fact as a SELF-CONTAINED statement: "
    "resolve this/that/the above from the context (e.g. \"prefers short bullet-point answers "
    "with food examples\"), never \"likes the above style\".\n"
    'Usually the answer is {"facts": []}.'
)

_TUTOR_CHARS = 800      # enough to recognise a style; not the whole lecture
_STUDENT_CHARS = 300


def worth_reading(message: str, context: Sequence[PriorTurn]) -> bool:
    """Skip trivial messages cheaply. A short reply still counts when it ANSWERS a
    question the tutor just asked ("Shorter answers from now on?" → "yes")."""
    if len(message.split()) >= 3:
        return True
    last = next((t for t in reversed(context) if t.role == "assistant"), None)
    return bool(message.strip()) and last is not None and last.text.rstrip().endswith("?")


def _context_block(context: Sequence[PriorTurn]) -> str:
    lines = []
    for t in context:
        limit = _TUTOR_CHARS if t.role == "assistant" else _STUDENT_CHARS
        text = " ".join(t.text.split())
        text = text if len(text) <= limit else text[:limit] + " …"
        lines.append(f"{'Tutor' if t.role == 'assistant' else 'Student'}: {text}")
    return "\n".join(lines) or "(none)"


class MemoryExtractor(ABC):
    @abstractmethod
    def extract(self, message: str, context: Sequence[PriorTurn] = ()) -> list[Candidate]:
        """Candidate facts; ``[]`` when nothing durable was said or on any failure.
        ``context`` = the messages just before ``message`` (oldest first)."""


def _unit(value, default: float) -> float:
    try:
        return min(1.0, max(0.0, float(value)))
    except (TypeError, ValueError):
        return default


class LLMExtractor(MemoryExtractor):
    def __init__(self, llm: LLMClient, *, max_candidates: int = 5) -> None:
        self.llm = llm
        self.max_candidates = max_candidates

    def extract(self, message, context=()):
        if not worth_reading(message, context):
            return []
        prompt = (f"RECENT CONVERSATION (context only):\n{_context_block(context)}\n\n"
                  f"Student's NEW message: {message[:1500]}\n\nJSON:")
        try:
            data = extract_json(self.llm.complete(prompt, system=_SYSTEM, temperature=0.0).text)
        except Exception:
            return []
        raw = data.get("facts") if isinstance(data, dict) else None
        if raw is None and isinstance(data, dict):
            raw = data.get("ops")                     # tolerate the v1 shape
        out: list[Candidate] = []
        for f in raw or []:
            if not isinstance(f, dict) or f.get("kind") not in MEMORY_KINDS:
                continue
            content = " ".join(str(f.get("content") or "").split())[:300]
            if not content:
                continue
            explicit = f.get("explicit") is True
            out.append(Candidate(kind=f["kind"], content=content,
                                 confidence=_unit(f.get("confidence"), 0.7),
                                 importance=1.0 if explicit else _unit(f.get("importance"), 0.5),
                                 explicit=explicit))
            if len(out) >= self.max_candidates:
                break
        return out


class NoExtractor(MemoryExtractor):
    def extract(self, message, context=()):
        return []


def build_extractor(provider: str, *, llm: LLMClient | None, max_candidates: int = 5) -> MemoryExtractor:
    if provider == "none" or llm is None:
        return NoExtractor()
    if provider == "llm":
        return LLMExtractor(llm, max_candidates=max_candidates)
    raise ValueError(f"unknown memory extractor {provider!r}; known: ['llm', 'none']")
