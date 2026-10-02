"""IntentRouter — decide which skill a user turn is asking for.

This is the chatbot's switchboard. Today it separates "answer my question" from
"make me a quiz"; the same machinery routes every capability added later, because
it is driven by the registered :class:`SkillSpec`s, not by hard-coded keywords.

Layers, cheapest first — each a reason the common case costs nothing:

1. **Awaiting** — the previous assistant turn asked the user for a missing slot
   ("What topic?"); a short reply goes straight back to that skill. Multi-turn
   skills need no special casing in the pipeline.
2. **Follow-up** — "another one", "make it harder" right after a skill ran are
   routed back to that skill (its :attr:`SkillSpec.followups`).
3. **Lexical gate** — each skill's trigger regexes score the message. No skill
   scores above the gate → it is a plain question: **zero extra LLM calls**, which
   is ~all traffic.
4. **LLM classification** (fast model) — only for gated turns. Sees the skill
   catalogue (descriptions, examples, *counter*-examples), the recent turns and the
   artifacts already made, and returns ``{intent, confidence, slots}``. This is what
   resolves "quiz me on *this*", tells "what is a quiz?" (a question) from "quiz me"
   (a command), and pulls out topic/count/difficulty.
5. **Thresholding** — below ``min_confidence`` the skill does not run; the turn is a
   normal answer. Acting on a guess is worse than answering.

Every decision is a :class:`RoutedIntent` that is streamed to the UI and stored on
the assistant message (``meta.intent``) — the raw material for measuring and
improving routing later. Providers self-register like every other pluggable stage.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ...config import ChatIntentConfig
from ...domain.chat import ChatMessage, Role
from ...providers.base import LLMClient
from ...util import extract_json
from .skills.base import QA, RoutedIntent, SkillSpec


@dataclass(slots=True)
class RouterState:
    """What the router may know about the conversation beyond the message text."""

    awaiting: dict[str, Any] | None = None     # {"skill", "slot", "slots"} from the last turn
    last_skill: str | None = None              # the skill that produced the last turn
    artifacts_note: str = ""                   # "Quizzes made so far: …" for the classifier
    history: list[ChatMessage] = field(default_factory=list)


class IntentRouter(ABC):
    @abstractmethod
    def route(self, message: str, state: RouterState) -> RoutedIntent:
        """Decide this turn's skill. Never raises: any failure routes to ``qa``."""

    @classmethod
    def from_config(
        cls, cfg: ChatIntentConfig, *, llm: LLMClient, specs: list[SkillSpec]
    ) -> IntentRouter:  # pragma: no cover
        raise NotImplementedError


_REGISTRY: dict[str, type[IntentRouter]] = {}


def register_intent_router(name: str) -> Callable[[type[IntentRouter]], type[IntentRouter]]:
    def deco(cls: type[IntentRouter]) -> type[IntentRouter]:
        _REGISTRY[name] = cls
        return cls

    return deco


def build_intent_router(
    cfg: ChatIntentConfig, *, llm: LLMClient, specs: list[SkillSpec]
) -> IntentRouter:
    try:
        cls = _REGISTRY[cfg.provider]
    except KeyError:
        available = ", ".join(sorted(_REGISTRY)) or "<none>"
        raise ValueError(
            f"Unknown intent provider {cfg.provider!r}. Available: {available}."
        ) from None
    return cls.from_config(cfg, llm=llm, specs=specs)


# ── shared pre-LLM layers ────────────────────────────────────────────────────
_CANCEL = re.compile(r"^\s*(cancel|never ?mind|forget it|no thanks?|stop|skip|not now)\b", re.IGNORECASE)
_AWAIT_MAX_WORDS = 12   # a longer reply is a new request, not an answer to our question


def _awaiting_route(message: str, state: RouterState, specs: dict[str, SkillSpec]) -> RoutedIntent | None:
    aw = state.awaiting
    if not aw or aw.get("skill") not in specs:
        return None
    if _CANCEL.match(message):
        return RoutedIntent(QA, 1.0, {}, "awaiting", "user cancelled the pending request")
    words = message.split()
    if len(words) > _AWAIT_MAX_WORDS or message.rstrip().endswith("?"):
        return None   # looks like a fresh question; let normal routing decide
    slots = dict(aw.get("slots") or {})
    slots[aw.get("slot", "")] = message.strip()
    return RoutedIntent(aw["skill"], 0.95, slots, "awaiting",
                        f"answering the pending {aw.get('slot')!r} question")


def _scores(message: str, state: RouterState, specs: dict[str, SkillSpec]) -> dict[str, float]:
    return {n: sp.lexical_score(message, after_this_skill=(state.last_skill == n))
            for n, sp in specs.items()}


# ── providers ────────────────────────────────────────────────────────────────
@register_intent_router("none")
class NoRouter(IntentRouter):
    """Everything is a plain answer — the pre-skills behaviour, and a kill switch."""

    @classmethod
    def from_config(cls, cfg, *, llm, specs) -> NoRouter:
        return cls()

    def route(self, message: str, state: RouterState) -> RoutedIntent:
        return RoutedIntent()


@register_intent_router("rules")
class RulesRouter(IntentRouter):
    """Lexical triggers only: free and fully deterministic, but it cannot resolve
    "this" or tell a command from a question about the thing. Acts only on strong
    triggers, and a skill's ``negatives`` veto it."""

    def __init__(self, cfg: ChatIntentConfig, specs: list[SkillSpec]) -> None:
        self.cfg = cfg
        self.specs = {s.name: s for s in specs}

    @classmethod
    def from_config(cls, cfg, *, llm, specs) -> RulesRouter:
        return cls(cfg, specs)

    def route(self, message: str, state: RouterState) -> RoutedIntent:
        try:
            if (r := _awaiting_route(message, state, self.specs)) is not None:
                return r
            return self._best(message, state)
        except Exception:
            return RoutedIntent()

    def _best(self, message: str, state: RouterState) -> RoutedIntent:
        scores = {n: sc for n, sc in _scores(message, state, self.specs).items()
                  if sc >= self.cfg.min_confidence and not self.specs[n].vetoed(message)}
        if not scores:
            return RoutedIntent()
        name = max(scores, key=scores.get)
        src = "followup" if state.last_skill == name and not any(
            t.matches(message) for t in self.specs[name].triggers) else "rules"
        return RoutedIntent(name, scores[name], {}, src, "matched a strong trigger")


_CLASSIFIER_SYSTEM = (
    "You are the intent router for a school study assistant. Decide which SKILL the "
    "user's latest message is asking for. Choose a skill only when the user clearly "
    "wants that thing DONE; a question ABOUT the thing (\"what is a quiz?\", \"how do "
    "I write MCQs?\") is a normal question, i.e. `qa`. Resolve references like \"this\" "
    "or \"it\" from the conversation so every slot is self-contained. Output ONLY JSON: "
    "{\"intent\": <name>, \"confidence\": <0..1>, \"slots\": {...}, \"reason\": <short>}."
)


def _catalogue(specs: dict[str, SkillSpec]) -> str:
    lines = [f"- {QA}: answer a question, explain, summarise, discuss — anything not below."]
    for sp in specs.values():
        lines.append(f"- {sp.name}: {sp.description}")
        for ex in sp.examples:
            lines.append(f'    e.g. "{ex}"')
        for ex in sp.counter_examples:
            lines.append(f'    NOT {sp.name}: "{ex}"  → {QA}')
        if sp.slots:
            lines.append("    slots: " + "; ".join(f"{k} = {v}" for k, v in sp.slots.items()))
    return "\n".join(lines)


@register_intent_router("hybrid")
class HybridRouter(IntentRouter):
    """Lexical gate → LLM classification → threshold (see the module docstring)."""

    def __init__(self, cfg: ChatIntentConfig, specs: list[SkillSpec], llm: LLMClient) -> None:
        self.cfg = cfg
        self.specs = {s.name: s for s in specs}
        self.llm = llm
        self._rules = RulesRouter(cfg, specs)

    @classmethod
    def from_config(cls, cfg, *, llm, specs) -> HybridRouter:
        return cls(cfg, specs, llm)

    GATE = 0.3  # a trigger at least this weak is worth an LLM look

    def route(self, message: str, state: RouterState) -> RoutedIntent:
        try:
            if (r := _awaiting_route(message, state, self.specs)) is not None:
                return r
            scores = _scores(message, state, self.specs)
            if self.cfg.gate == "lexical" and max(scores.values(), default=0.0) < self.GATE:
                return RoutedIntent()          # ordinary question: no LLM call at all
            routed = self._classify(message, state)
            if routed is None:                 # LLM down / unparseable → deterministic fallback
                return self._rules.route(message, state)
            return routed
        except Exception:
            return RoutedIntent()

    def _classify(self, message: str, state: RouterState) -> RoutedIntent | None:
        try:
            raw = self.llm.complete(self._prompt(message, state), system=_CLASSIFIER_SYSTEM,
                                    temperature=0.0).text
            data = extract_json(raw)
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        name = str(data.get("intent", QA)).strip().lower()
        try:
            conf = max(0.0, min(1.0, float(data.get("confidence", 0.0))))
        except (TypeError, ValueError):
            conf = 0.0
        reason = str(data.get("reason", ""))[:200]
        if name != QA and name not in self.specs:
            return RoutedIntent(QA, 1.0, {}, "llm", f"unknown intent {name!r} ignored")
        if name == QA:
            return RoutedIntent(QA, conf or 1.0, {}, "llm", reason)
        if conf < self.cfg.min_confidence:
            return RoutedIntent(QA, conf, {}, "llm", f"low confidence for {name}: {reason}")
        allowed = self.specs[name].slots
        slots = {k: v for k, v in (data.get("slots") or {}).items()
                 if k in allowed and v not in (None, "", [])}
        return RoutedIntent(name, conf, slots, "llm", reason)

    def _prompt(self, message: str, state: RouterState) -> str:
        parts = ["Skills:\n" + _catalogue(self.specs)]
        recent = [m for m in state.history if m.role in (Role.user, Role.assistant)]
        recent = recent[-max(0, self.cfg.history_turns) * 2:]
        if recent:
            parts.append("Recent conversation:\n" + "\n".join(
                f"{m.role.value.capitalize()}: {m.content[:300]}" for m in recent))
        if state.artifacts_note:
            parts.append(state.artifacts_note)
        parts.append(f"Latest user message: {message}\n\nJSON:")
        return "\n\n".join(parts)
