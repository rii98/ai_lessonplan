"""Skills — the chatbot's capabilities, and how a turn finds the right one.

The chat started as one capability: *answer a question from the knowledge base*. A
**skill** is any other thing the assistant can DO for a user — make a quiz, build
flashcards, draft a study plan, explain it simpler, translate, compare two topics.
Each is a small self-contained class that:

1. declares a :class:`SkillSpec` — its name, a one-line description, example
   utterances, the *slots* (parameters) it can take, and cheap lexical triggers; and
2. implements :meth:`Skill.run`, yielding the same :class:`ChatEvent` stream the
   answer pipeline does (status, tokens, an artifact…).

The :class:`~lessonforge.services.chat.intent.IntentRouter` is built FROM the
registered specs, so **adding a capability is one new class + one
``@register_skill`` decorator — the router, its classifier prompt, the pipeline and
the API need no edit.** That is the point of this module: intent detection is not a
hard-coded ``if "quiz" in message`` — it is data the skills contribute.

``qa`` (a grounded answer) is the implicit default skill: anything no other skill
claims is answered exactly as before.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any, ClassVar

from ....domain.chat import ChatMessage, Conversation
from ....providers.base import LLMClient
from ..events import ChatEvent

QA = "qa"  # the default skill: a grounded answer


@dataclass(frozen=True, slots=True)
class Trigger:
    """A cheap lexical cue. ``weight`` is how strongly a match implies the skill:
    ~0.9 for an unmistakable command ("quiz me"), ~0.4 for a word that merely
    *might* mean it ("question"). The hybrid router sends anything above its gate
    to the LLM for confirmation; the rules-only router acts on strong ones."""

    pattern: str
    weight: float = 0.9

    def matches(self, text: str) -> bool:
        return re.search(self.pattern, text, re.IGNORECASE | re.UNICODE) is not None


@dataclass(frozen=True, slots=True)
class SkillSpec:
    name: str
    description: str                          # one line; goes into the classifier prompt
    examples: tuple[str, ...] = ()            # utterances that SHOULD route here
    counter_examples: tuple[str, ...] = ()    # look similar but should NOT (sharpens the LLM)
    slots: dict[str, str] = field(default_factory=dict)   # slot name → description
    triggers: tuple[Trigger, ...] = ()        # fire the lexical gate
    negatives: tuple[str, ...] = ()           # patterns that veto the rules-only router
    followups: tuple[Trigger, ...] = ()       # short follow-ups ("another one") — only
                                              # considered when the previous turn used this skill

    def lexical_score(self, text: str, *, after_this_skill: bool = False) -> float:
        """Best trigger weight that matches ``text`` (0 when none)."""
        best = max((t.weight for t in self.triggers if t.matches(text)), default=0.0)
        if after_this_skill:
            best = max(best, max((t.weight for t in self.followups if t.matches(text)), default=0.0))
        return best

    def vetoed(self, text: str) -> bool:
        return any(re.search(n, text, re.IGNORECASE | re.UNICODE) for n in self.negatives)


@dataclass(slots=True)
class RoutedIntent:
    """The router's decision for one turn."""

    skill: str = QA
    confidence: float = 1.0
    slots: dict[str, Any] = field(default_factory=dict)
    source: str = "default"   # default | rules | llm | awaiting | followup
    reason: str = ""

    def meta(self) -> dict[str, Any]:
        return {"skill": self.skill, "confidence": round(self.confidence, 3),
                "slots": self.slots, "source": self.source, "reason": self.reason}


@dataclass(slots=True)
class SkillDeps:
    """Everything a skill may need, injected once by the container (so a skill never
    reaches into global state and is trivially faked in tests)."""

    llm: LLMClient                 # the reasoning model
    llm_fast: LLMClient            # the cheap model
    grounding: Any = None          # GroundingRetriever | None
    config: Any = None             # ChatConfig


@dataclass(slots=True)
class SkillContext:
    conversation: Conversation
    message: str
    routed: RoutedIntent
    history: list[ChatMessage]     # recent turns, oldest first
    summary: str = ""
    deps: SkillDeps | None = None


class Skill(ABC):
    """One capability. Subclasses set ``spec`` and implement :meth:`run`."""

    spec: ClassVar[SkillSpec]

    def __init__(self, deps: SkillDeps) -> None:
        self.deps = deps

    @abstractmethod
    def run(self, ctx: SkillContext) -> Iterator[ChatEvent]:
        """Do the thing, yielding ``status`` / ``token`` / ``artifact`` / ``awaiting``
        events. Must not raise for expected failures — say so in a ``token`` instead."""

    def fill_slot(self, slot: str, text: str) -> Any:
        """Turn the user's free-text reply to an ``awaiting`` question into the slot's
        value. Default: the text itself."""
        return text.strip()


SKILL_REGISTRY: dict[str, type[Skill]] = {}


def register_skill(cls: type[Skill]) -> type[Skill]:
    name = cls.spec.name
    if name == QA:
        raise ValueError(f"{QA!r} is the built-in default skill and cannot be registered")
    if name in SKILL_REGISTRY:
        raise ValueError(f"skill {name!r} already registered as {SKILL_REGISTRY[name]!r}")
    SKILL_REGISTRY[name] = cls
    return cls


def build_skills(deps: SkillDeps, *, enabled: Callable[[str], bool] | None = None) -> dict[str, Skill]:
    """Instantiate every registered skill (optionally filtered by config)."""
    return {n: c(deps) for n, c in sorted(SKILL_REGISTRY.items()) if enabled is None or enabled(n)}
