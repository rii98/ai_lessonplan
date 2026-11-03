"""MemoryPolicy — what may be remembered about a learner. Our users are minors, so the
default is data minimisation, enforced in code (and unit-tested), not left to a prompt:

- **allowlist** of kinds (preference, learning_need, interest, goal, episode);
- a **denylist** of personal or sensitive content: contact details, names and
  addresses, family, health, religion/caste/ethnicity, money, marks and attendance,
  credentials, and anything about other people;
- a confidence floor, a length cap, and de-duplication.

The extractor proposes; this policy decides — twice: once on each candidate BEFORE it is
reconciled (refused content never reaches a second model), and again on the reconciled op
(a merged sentence is new text). The digest's lines are checked too. A vetoed op is
simply dropped.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod

from ...domain.memory import MEMORY_KINDS, Candidate, MemoryItem, MemoryOp

_I = re.IGNORECASE | re.UNICODE
DENY = [
    re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+", _I),                                   # email
    re.compile(r"(?:\+?\d[\s-]?){7,}", _I),                                        # phone-like numbers
    re.compile(r"\b(my|his|her|their) name is\b|\bi am called\b|\bcalled me\b", _I),
    re.compile(r"\b(address|street|tole|ward no|house no|lives? (?:at|in|near))\b", _I),
    re.compile(r"\b(mother|father|mom|mum|dad|parents?|brother|sister|uncle|aunt|grand(?:ma|pa|mother|father)|"
               r"family|आमा|बुबा|बाबा|दाइ|दिदी)\b", _I),
    re.compile(r"\b(sick|ill|illness|disease|disorder|diagnos\w*|medic\w*|hospital|therapy|depress\w*|"
               r"anxiety|adhd|autis\w*|disab\w*|pregnan\w*)\b", _I),
    re.compile(r"\b(religio\w*|caste|ethnic\w*|hindu|muslim|christian|buddhist|dalit|brahmin|chhetri|janajati)\b", _I),
    re.compile(r"\b(password|pin code|otp|login)\b", _I),
    re.compile(r"\b(marks?|scored?|percent(?:age)?|gpa|failed (?:the|my) exam|attendance|rank(?:ed)?)\b", _I),
    re.compile(r"\b(money|salary|rupees|rs\.?|poor|rich)\b", _I),
    re.compile(r"\b(boyfriend|girlfriend|crush|dating)\b", _I),
    re.compile(r"\b(friend|classmate|teacher)\s+\w+\s+(is|was|has|said)\b", _I),   # about other people
]


def sensitive(text: str) -> bool:
    return any(p.search(text) for p in DENY)


# A note read weeks later must make sense with no conversation around it.
VAGUE = re.compile(r"\b(the above|above (?:style|one|way|format)|(?:this|that|same) (?:style|way|one|format|thing)|"
                   r"like (?:this|that|before)|as before|the previous (?:one|answer|style)|it)\b\s*\.?$|"
                   r"\b(the above|this style|that style|that one|this one|the same way)\b", _I)


def vague(text: str) -> bool:
    """Unresolved references ("likes the above style") — meaningless out of context."""
    return bool(VAGUE.search(text))


class MemoryPolicy(ABC):
    """Decides what may be stored. ``reason`` returns why something is refused (``None``
    = allowed) so a learner who types a note themselves can be told."""

    @abstractmethod
    def candidate_reason(self, candidate: Candidate) -> str | None:
        """Pre-filter BEFORE reconciliation: refused content never even reaches the
        reconciling model."""

    @abstractmethod
    def reason(self, op: MemoryOp, live: list[MemoryItem]) -> str | None:
        """Final check on a reconciled op (merged content is re-checked)."""

    def allow(self, op: MemoryOp, live: list[MemoryItem]) -> bool:
        return self.reason(op, live) is None

    def allow_line(self, line: str) -> bool:
        """A line of a synthesised digest — the same content rules apply to anything
        the model writes about the learner."""
        return not sensitive(line)


class AllowlistPolicy(MemoryPolicy):
    def __init__(self, *, min_confidence: float = 0.6, max_chars: int = 200) -> None:
        self.min_confidence = min_confidence
        self.max_chars = max_chars

    def _content_reason(self, kind: str | None, content: str, confidence: float) -> str | None:
        content = content.strip()
        if not (3 <= len(content) <= self.max_chars):
            return "too short or too long"
        if kind not in MEMORY_KINDS:
            return "not a kind of note we keep"
        if confidence < self.min_confidence:
            return "not certain enough"
        if sensitive(content):
            return "personal or sensitive information is never remembered"
        if vague(content):
            return "doesn't make sense on its own"
        return None

    def candidate_reason(self, candidate):
        return self._content_reason(candidate.kind, candidate.content, candidate.confidence)

    def reason(self, op, live):
        ids = {i.id for i in live}
        if op.op in ("update", "retire") and op.target_id not in ids:
            return "unknown memory"
        if op.op == "retire":
            return None
        kind = op.kind or next((i.kind for i in live if i.id == op.target_id), None)
        why = self._content_reason(kind, op.content, op.confidence)
        if why:
            return why
        if op.op == "add":
            norm = op.content.casefold().strip().rstrip(".")
            if any(i.content.casefold().strip().rstrip(".") == norm for i in live):
                return "already remembered"
        return None
