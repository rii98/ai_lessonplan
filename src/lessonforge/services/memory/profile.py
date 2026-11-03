"""The always-on profile layer — who the learner is, in a few lines, every turn.

Two providers behind one port (``chat.memory.long_term.profile``):

- ``ranked`` — deterministic: the profile kinds, explicit requests first, then by
  importance and recency, under a line and character budget. Free, instant, and always
  available: it is also the fallback whenever no valid digest exists.
- ``llm`` — a synthesised **digest** (ChatGPT/Claude-style "what I know about you"):
  the fast model compresses the live memories into a few deduplicated bullets. Built OFF
  the request path after enough writes (or when old); every line is policy-checked like
  any other model-written text about a minor; never shown once something it may mention
  has been forgotten, retired or rewritten (``MemoryState.digest_valid``).
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import datetime

from ...domain.memory import MemoryItem
from ...providers.base import LLMClient
from .recall import recency

log = logging.getLogger("lessonforge.memory.profile")

_KIND_ORDER = {"preference": 0, "goal": 1, "interest": 2, "learning_need": 3, "episode": 4}


def ranked_profile(items: list[MemoryItem], *, kinds: tuple[str, ...] | list[str], max_lines: int,
                   now: datetime, half_life_days: float = 30.0) -> list[MemoryItem]:
    pool = [i for i in items if i.kind in kinds]
    pool.sort(key=lambda i: (not i.source.get("explicit"),
                             -(0.7 * i.importance + 0.3 * recency(i, now, half_life_days))))
    chosen = pool[:max_lines]
    chosen.sort(key=lambda i: _KIND_ORDER.get(i.kind, 9))
    return chosen


def digest_lines(text: str) -> list[str]:
    """The bullets of a stored digest (headings and blank lines dropped)."""
    out = []
    for raw in text.splitlines():
        line = raw.strip().lstrip("-*•").strip()
        if line and not line.endswith(":") and not line.startswith("#"):
            out.append(line)
    return out


class ProfileSynthesizer(ABC):
    @abstractmethod
    def synthesize(self, items: list[MemoryItem], *, max_chars: int) -> str | None:
        """A digest, or ``None`` (not available / failed → the ranked profile is used)."""


class NoSynthesizer(ProfileSynthesizer):
    def synthesize(self, items, *, max_chars):
        return None


_SYSTEM = (
    "You write the standing profile a tutor sees before every lesson with this school "
    "student. Compress the memories into at most 6 short bullet lines, most useful first: "
    "how to teach them (language, format, pace, examples), their goals, interests to use in "
    "examples, what they find hard. Merge duplicates; when two memories disagree, keep the "
    "newer one. Use ONLY what the memories say — invent nothing, add no names or personal "
    "details. Each bullet is self-contained, third person. Output only the bullets, each "
    "starting with '- '."
)


class LLMProfileSynthesizer(ProfileSynthesizer):
    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm

    def synthesize(self, items, *, max_chars):
        if not items:
            return None
        lines = "\n".join(f"- [{i.kind}] (importance {i.importance:.1f}, {i.valid_from:%Y-%m-%d}) {i.content}"
                          for i in items)
        try:
            text = self.llm.complete(f"Memories (newest first):\n{lines}\n\nProfile:",
                                     system=_SYSTEM, temperature=0.0).text.strip()
        except Exception:
            log.exception("profile synthesis failed")
            return None
        bullets = digest_lines(text)
        if not bullets:
            return None
        out, used = [], 0
        for b in bullets:
            if used + len(b) + 3 > max_chars:
                break
            out.append(f"- {b}")
            used += len(b) + 3
        return "\n".join(out) or None


def build_synthesizer(provider: str, *, llm: LLMClient | None) -> ProfileSynthesizer:
    if provider == "ranked" or llm is None:
        return NoSynthesizer()
    if provider == "llm":
        return LLMProfileSynthesizer(llm)
    raise ValueError(f"unknown memory profile {provider!r}; known: ['llm', 'ranked']")
