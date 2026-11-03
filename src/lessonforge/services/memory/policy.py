"""MemoryPolicy — what may be remembered about a learner. Our users are minors, so the
default is data minimisation, enforced in code (and unit-tested), not left to a prompt:

- **allowlist** of kinds (preference, learning_need, interest, goal, episode);
- a **denylist** of personal or sensitive content: contact details, names and
  addresses, family, health, religion/caste/ethnicity, money, marks and attendance,
  credentials, and anything about other people;
- a confidence floor, a length cap, and de-duplication.

The extractor proposes; this policy decides. A vetoed op is simply dropped.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod

from ...domain.memory import MEMORY_KINDS, MemoryItem, MemoryOp

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


class MemoryPolicy(ABC):
    @abstractmethod
    def allow(self, op: MemoryOp, existing: list[MemoryItem]) -> bool: ...


class AllowlistPolicy(MemoryPolicy):
    def __init__(self, *, min_confidence: float = 0.6, max_items: int = 300) -> None:
        self.min_confidence = min_confidence
        self.max_items = max_items

    def allow(self, op, existing):
        if op.op == "delete":
            return op.target_id in {i.id for i in existing}
        content = op.content.strip()
        if not (3 <= len(content) <= 200) or op.kind not in MEMORY_KINDS:
            return False
        if op.confidence < self.min_confidence or sensitive(content):
            return False
        if op.op == "update":
            return op.target_id in {i.id for i in existing}
        norm = content.casefold().rstrip(".")
        if any(i.content.casefold().rstrip(".") == norm for i in existing):
            return False                                   # already remembered
        return len(existing) < self.max_items
