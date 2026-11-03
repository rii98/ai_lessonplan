"""MemoryExtractor — propose memory changes from what the learner just said (Mem0's
extract → compare → ADD/UPDATE/DELETE/NOOP pattern).

Only the learner's OWN words are read (never the assistant's reply, which would let
the model "remember" what it said). The model sees the existing memories most similar
to the new message, so a changed preference becomes an UPDATE of the old one rather
than a contradictory second item. Conservative by instruction: durable facts about
how this learner learns — not what they asked about (that's in the transcript).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence

from ...domain.memory import MEMORY_KINDS, MemoryItem, MemoryOp, PriorTurn
from ...providers.base import LLMClient
from ...util import extract_json

_SYSTEM = (
    "You maintain a small memory about a school student to personalise tutoring. Read the "
    "student's message and decide if it reveals a DURABLE fact about how they learn: a "
    "preference (language, examples, pace, format), a learning_need (something they find "
    "hard), an interest (a hobby to use in examples), or a goal (an exam, a target). "
    "Ignore questions about the subject, one-off requests, and greetings. NEVER record "
    "names, contact details, family, health, religion, caste, money, marks, or anything "
    "about other people. Output ONLY JSON: {\"ops\": [...]} where each op is one of\n"
    '  {"op": "add", "kind": "preference|learning_need|interest|goal", "content": '
    '"<short third-person statement>", "confidence": 0..1}\n'
    '  {"op": "update", "id": "<existing id>", "content": "<new statement>", "confidence": 0..1}\n'
    '  {"op": "delete", "id": "<existing id>"}   (the student contradicted it)\n'
    "The RECENT CONVERSATION is context only: use it to understand what the student's new "
    "message refers to (\"the above style\", \"yes, that one\", \"like this\"). Record ONLY what "
    "the STUDENT states or agrees to — never something the tutor said, offered or assumed "
    "that the student did not accept. Write every memory as a SELF-CONTAINED statement: "
    "resolve this/that/the above from the context (e.g. \"prefers short bullet-point answers "
    "with food examples\"), never \"likes the above style\".\n"
    'Usually the answer is {"ops": []}.'
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
    def extract(self, message: str, similar: list[MemoryItem],
                context: Sequence[PriorTurn] = ()) -> list[MemoryOp]:
        """Proposed ops; ``[]`` when nothing durable was said or on any failure.
        ``context`` = the messages just before ``message`` (oldest first)."""


class LLMExtractor(MemoryExtractor):
    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm

    def extract(self, message, similar, context=()):
        if not worth_reading(message, context):
            return []
        known = "\n".join(f'- id={m.id} [{m.kind}] "{m.content}"' for m in similar) or "(none)"
        prompt = (f"Existing memories:\n{known}\n\n"
                  f"RECENT CONVERSATION (context only):\n{_context_block(context)}\n\n"
                  f"Student's NEW message: {message[:1500]}\n\nJSON:")
        try:
            data = extract_json(self.llm.complete(prompt, system=_SYSTEM, temperature=0.0).text)
        except Exception:
            return []
        ops: list[MemoryOp] = []
        by_id = {m.id: m for m in similar}
        for raw in (data.get("ops") if isinstance(data, dict) else None) or []:
            if not isinstance(raw, dict):
                continue
            kind = raw.get("kind")
            target = raw.get("id")
            if raw.get("op") in ("update", "delete") and target in by_id:
                kind = kind if kind in MEMORY_KINDS else by_id[target].kind
            try:
                ops.append(MemoryOp(op=raw.get("op"), kind=kind if kind in MEMORY_KINDS else None,
                                    content=str(raw.get("content") or "")[:300], target_id=target,
                                    confidence=float(raw.get("confidence", 0.7))))
            except Exception:  # noqa: S112 - one malformed op must not drop the others
                continue
        return ops


class NoExtractor(MemoryExtractor):
    def extract(self, message, similar, context=()):
        return []


def build_extractor(provider: str, *, llm: LLMClient | None) -> MemoryExtractor:
    if provider == "none" or llm is None:
        return NoExtractor()
    if provider == "llm":
        return LLMExtractor(llm)
    raise ValueError(f"unknown memory extractor {provider!r}; known: ['llm', 'none']")
