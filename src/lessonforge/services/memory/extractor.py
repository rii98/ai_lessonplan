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

from ...domain.memory import MEMORY_KINDS, MemoryItem, MemoryOp
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
    'Usually the answer is {"ops": []}.'
)


class MemoryExtractor(ABC):
    @abstractmethod
    def extract(self, message: str, similar: list[MemoryItem]) -> list[MemoryOp]:
        """Proposed ops; ``[]`` when nothing durable was said or on any failure."""


class LLMExtractor(MemoryExtractor):
    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm

    def extract(self, message, similar):
        if len(message.split()) < 3:        # "ok", "yes" — nothing to remember
            return []
        known = "\n".join(f'- id={m.id} [{m.kind}] "{m.content}"' for m in similar) or "(none)"
        prompt = f"Existing memories:\n{known}\n\nStudent's message: {message[:1500]}\n\nJSON:"
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
    def extract(self, message, similar):
        return []


def build_extractor(provider: str, *, llm: LLMClient | None) -> MemoryExtractor:
    if provider == "none" or llm is None:
        return NoExtractor()
    if provider == "llm":
        return LLMExtractor(llm)
    raise ValueError(f"unknown memory extractor {provider!r}; known: ['llm', 'none']")
