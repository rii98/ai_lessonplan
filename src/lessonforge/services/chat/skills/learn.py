"""The learn skill — the router's door into learn mode (docs/LEARN_MODE.md §2.1).

Learn mode is a multi-turn *session*, not a one-shot skill, so this skill only starts
one; afterwards the session owns the conversation's turns (``LearnService``).

Students should choose the slower, scaffolded path themselves, so a routed request
**offers** a lesson (a ``learn_offer`` event the UI renders as a "Start lesson"
button) — unless the student asked unmistakably ("teach me…", "step by step"), in
which case it starts right away.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

from ..events import ChatEvent
from .base import Skill, SkillContext, SkillSpec, Trigger, register_skill

_EXPLICIT = re.compile(
    r"\b(teach me|tutor me|learn mode|step[\s-]+by[\s-]+step|from the (?:basics|ground up|start)|"
    r"help me (?:learn|understand))\b", re.IGNORECASE)

SPEC = SkillSpec(
    name="learn",
    description=("Start a step-by-step tutoring lesson that builds the student's understanding "
                 "with guiding questions, hints and interactive checks, instead of just giving "
                 "the answer. Use it when the student wants to LEARN or UNDERSTAND a topic "
                 "deeply, says they don't get it, or asks to be taught."),
    examples=(
        "teach me photosynthesis",
        "help me understand osmosis",
        "I don't get fractions at all, can you teach me?",
        "explain the water cycle step by step so I really understand",
        "I want to learn about electricity from the basics",
        "tutor me on acids and bases",
        "can you help me learn Newton's laws?",
    ),
    counter_examples=(
        "what is photosynthesis?",
        "explain photosynthesis",
        "quiz me on osmosis",
        "give me 5 questions on fractions",
        "make a lesson plan on the water cycle",
        "how do I teach fractions to my class?",
    ),
    slots={"topic": "what to learn, self-contained (resolve 'this'/'it' from the conversation)"},
    triggers=(
        Trigger(r"\b(?:teach|tutor)\s+me\b"),
        Trigger(r"\bhelp\s+me\s+(?:learn|understand)\b", 0.8),
        Trigger(r"\bstep[\s-]+by[\s-]+step\b", 0.6),
        Trigger(r"\bi\s+(?:don'?t|do\s+not)\s+(?:get|understand)\b", 0.5),
        Trigger(r"\b(?:i\s+want\s+to|let'?s)\s+learn\b", 0.8),
        Trigger(r"\blearn\s+mode\b"),
        Trigger(r"सिकाउ|बुझाउ", 0.8),
    ),
)


@register_skill
class LearnSkill(Skill):
    spec = SPEC

    def run(self, ctx: SkillContext) -> Iterator[ChatEvent]:
        topic = str(ctx.routed.slots.get("topic") or "").strip() or ctx.message.strip()
        learn = self.deps.learn if self.deps else None
        if learn is None:
            yield ChatEvent("token", "Learn mode isn't available right now — ask me anything "
                                     "about it and I'll explain.")
            return
        if not _EXPLICIT.search(ctx.message):
            yield ChatEvent("token", f"Would you like me to teach you **{topic}** step by step? "
                                     "I'll guide you with questions, hints and quick puzzles "
                                     "instead of just giving the answer.")
            yield ChatEvent("learn_offer", {"topic": topic[:200]})
            return
        from ...learn import scope_from

        scope = scope_from(ctx.filters, ctx.collections, allow_fallback=ctx.allow_fallback,
                           language=ctx.language)
        yield from learn.begin(ctx.conversation, topic[:200], scope, history=ctx.history)
