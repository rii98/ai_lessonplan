"""The quiz skill — "quiz me on photosynthesis" → an interactive quiz in the side panel.

It deliberately reuses the assessment foundation built for printed worksheets: the
same :class:`AssessmentSpec` blueprint, the same type-aware retrieval and the same
:class:`QuizGenerator` (with ``explanations=True`` so every question carries the
"why" shown as feedback). What is chat-specific is only (a) *understanding the
request* — slots, defaults, a sensible question mix — and (b) *where the result goes*:
a persisted :class:`ChatArtifact`, not a file.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterator
from typing import Any

from ....domain.assessment import AssessmentSpec, TypeSpec
from ....domain.chat import ChatArtifact
from ....domain.ldd import NormalizedBrief, QuestionType
from ....export.base import ArtifactKind
from ...artifact_generation import build_generator
from ...normalize import DIFFICULTIES, Q_TYPES, _snap, _to_int
from ..events import ChatEvent
from .base import RoutedIntent, Skill, SkillContext, SkillSpec, Trigger, register_skill

_QUESTION_WORDS = ("quiz|quizzes|test|questions?|mcqs?|problems?|exercises?")

SPEC = SkillSpec(
    name="quiz",
    description=("Make an interactive practice quiz the student can play right now, with "
                 "instant feedback. Use it when the user wants to be tested, asks for "
                 "practice questions, MCQs, or a quiz on something."),
    examples=(
        "quiz me on photosynthesis",
        "give me 5 practice questions on fractions",
        "test me on this",
        "ask me some questions about the water cycle",
        "i want to practise mcqs on force and motion",
        "another one, but harder",
        # indirect phrasings — students rarely say "quiz"; the LLM must generalise
        "help me revise osmosis",
        "challenge me on cells",
        "how well do I know the water cycle?",
        "I have a test tomorrow, can I try some problems?",
        "hit me with 10 on sound",
        "let me see if I understood this",
        "give me a mock paper for grade 8",
    ),
    counter_examples=(
        "what is a quiz?",
        "how do I write good MCQs for my class?",
        "explain photosynthesis",
        "what questions come in the SEE science exam?",
        "give me a lesson plan on fractions",
        "help me understand osmosis",
        "what should I revise for the science exam?",
        "when is the science test?",
    ),
    slots={
        "topic": "what to be quizzed on, self-contained (resolve 'this'/'it' from the conversation)",
        "count": "number of questions, an integer, if the user said one",
        "types": ("list of question types if the user named any: mcq, true_false, fill_blank, "
                  "matching, short_answer, long_answer, numerical, ordering"),
        "difficulty": "easy | medium | hard | mixed, if the user said one",
    },
    triggers=(
        Trigger(r"\b(?:quiz|test|examine|drill)\s+me\b"),
        Trigger(rf"\bgive\s+me\s+(?:an?\s+|some\s+|\d+\s+|a\s+few\s+)?(?:\w+\s+){{0,2}}?(?:{_QUESTION_WORDS})\b"),
        Trigger(r"\b(?:make|create|generate|prepare|write|set|build)\s+(?:me\s+)?(?:an?\s+|some\s+|\d+\s+)?(?:\w+\s+){0,2}?(?:quiz|quizzes|test|mcqs?|practice\s+questions?)\b"),
        Trigger(r"\bpracti[cs]e\s+(?:questions?|problems?|quiz|test|mcqs?|exercises?)\b"),
        Trigger(r"\bask\s+me\s+(?:some\s+|a\s+few\s+|\d+\s+)?(?:\w+\s+)?questions?\b"),
        Trigger(r"\b(?:i\s+want|let'?s|can\s+(?:you|i)|help\s+me)\s+(?:to\s+)?practi[cs]e\b"),
        Trigger(r"\bcheck\s+(?:my\s+)?(?:knowledge|understanding)\b"),
        Trigger(r"\bsee\s+how\s+much\s+i\s+(?:know|understand)\b"),
        Trigger(r"\bchallenge\s+me\b|\bhit\s+me\s+with\b|\bthrow\s+(?:some\s+)?\w+\s+at\s+me\b"),
        Trigger(r"\bhow\s+(?:well|much)\s+do\s+i\s+(?:know|remember|understand)\b"),
        Trigger(r"\blet\s+me\s+(?:see|check|find\s+out)\s+if\s+i\b|\bsee\s+if\s+i\s+(?:get|understood|know)\b"),
        Trigger(r"\bmock\s+(?:test|paper|exam|quiz)\b"),
        Trigger(r"\b(?:i(?:'d| would)?\s+(?:like|need|want)|can\s+i\s+(?:have|get|try)|let\s+me\s+try)\s+(?:some\s+|a\s+few\s+|\d+\s+)?(?:exercises?|problems?|questions?)\b"),
        Trigger(r"\b(?:revis(?:e|ion)|study\s+for|prepare\s+for)\b", 0.5),   # → the LLM decides
        Trigger(r"\b(?:exercises?|worksheet)\b", 0.4),
        Trigger(r"प्रश्न\s*(?:सोध|दे)|परीक्षा\s*(?:दिनुहोस्|लिनुहोस्)", 0.9),          # Nepali: "ask/give me questions"
        Trigger(r"\bprashna\b|क्विज|अभ्यास", 0.5),       # Romanised / Devanagari cues → LLM decides
        Trigger(r"\b(?:quiz|quizzes|mcqs?|questions?|practi[cs]e|exam|test)\b", 0.4),
    ),
    negatives=(
        rf"\b(?:what|who|why|when|which)\s+(?:is|are|was|were)\s+(?:a|an|the)?\s*(?:{_QUESTION_WORDS}|practice)\b",
        rf"\bhow\s+(?:do|can|should|to)\b.*\b(?:write|make|create|design|set)\b.*\b(?:{_QUESTION_WORDS})\b",
        r"\bexplain\b",
    ),
    followups=(
        Trigger(r"^\s*(?:another|one\s+more|more|again|harder|easier|a\s+harder|a\s+different|different|new)\b", 0.7),
    ),
)

# how a quiz is split when the user didn't say which types they want
_DEFAULT_TYPES_BY_COUNT = (
    (2, [QuestionType.mcq]),
    (4, [QuestionType.mcq, QuestionType.true_false]),
)


def coerce_slots(raw: dict[str, Any], *, default_count: int, max_count: int) -> dict[str, Any]:
    """Make model-extracted slots safe: clamp the count, keep only real types, snap
    the difficulty. Anything unusable is dropped (the defaults then apply)."""
    out: dict[str, Any] = {}
    topic = re.sub(r"\s+", " ", str(raw.get("topic") or "")).strip(" .?!\"'")
    if topic:
        out["topic"] = topic[:120]
    n = _to_int(raw.get("count"))
    out["count"] = max(1, min(max_count, n)) if n else default_count
    types = raw.get("types")
    if isinstance(types, str):
        types = re.split(r"[,/&]|\band\b", types)
    if isinstance(types, list):
        snapped = [_snap(t, Q_TYPES, "", "type")[0] for t in types if isinstance(t, str) and t.strip()]
        out["types"] = [QuestionType(t) for t in dict.fromkeys(snapped) if t in Q_TYPES]
    diff = raw.get("difficulty")
    if isinstance(diff, str) and diff.strip():
        low = diff.strip().lower()
        out["difficulty"] = "mixed" if low in ("mixed", "any", "mix", "varied") else \
            _snap(low, DIFFICULTIES, "mixed", "difficulty")[0]
    return out


def build_spec(count: int, types: list[QuestionType] | None, difficulty: str) -> AssessmentSpec:
    """The blueprint for a chat quiz: the user's types split evenly, else an
    MCQ-heavy mix that scales with the count. Explanations are always on — they are
    the feedback the player shows after each answer."""
    if types:
        k = len(types)
        counts = {t: count // k + (1 if i < count % k else 0) for i, t in enumerate(types)}
    else:
        base = next((ts for limit, ts in _DEFAULT_TYPES_BY_COUNT if count <= limit), None)
        if base is None:                      # 5+: mcq-heavy, then true/false, then fill-in
            mcq = max(1, round(count * 0.6))
            rest = count - mcq
            counts = {QuestionType.mcq: mcq, QuestionType.true_false: math.ceil(rest / 2),
                      QuestionType.fill_blank: rest // 2}
        elif len(base) == 1:
            counts = {base[0]: count}
        else:
            counts = {QuestionType.mcq: count - 1, QuestionType.true_false: 1}
    rows = [TypeSpec(type=t, count=c, difficulty=difficulty) for t, c in counts.items() if c > 0]
    return AssessmentSpec(types=rows, explanations=True)


def _jsonable(slots: dict[str, Any]) -> dict[str, Any]:
    return {k: [t.value for t in v] if k == "types" else v for k, v in slots.items()}


def _lead_in(title: str, n: int, difficulty: str) -> str:
    mix = "" if difficulty == "mixed" else f" ({difficulty})"
    return (f"I've made a **{n}-question quiz{mix}** on **{title}** — it's open on the right. "
            "I'll check each answer and tell you why. When you're done you can retry the "
            "ones you missed, or ask me for another quiz (harder, easier, or a new topic).")


@register_skill
class QuizSkill(Skill):
    spec = SPEC

    def run(self, ctx: SkillContext) -> Iterator[ChatEvent]:
        cfg = self.deps.config.quiz
        slots = coerce_slots(ctx.routed.slots, default_count=cfg.default_count,
                             max_count=cfg.max_count)
        topic = slots.get("topic")
        if not topic:
            yield ChatEvent("token", "Happy to quiz you! What topic should it cover?")
            yield ChatEvent("awaiting", {"skill": self.spec.name, "slot": "topic",
                                         "slots": {k: v for k, v in ctx.routed.slots.items()
                                                   if k != "topic"}})
            return

        yield ChatEvent("status", f"Writing your quiz on {topic}…")
        d = ctx.conversation.defaults
        brief = NormalizedBrief(topic=topic, grade=d.grade or cfg.default_grade,
                                subject=d.subject or "General", language="en-ne")
        difficulty = slots.get("difficulty", "mixed")
        spec = build_spec(slots["count"], slots.get("types"), difficulty)
        try:
            gen = build_generator(ArtifactKind.quiz, llm=self.deps.llm,
                                  grounding=self.deps.grounding, max_repairs=1)
            quiz = gen.generate(brief, spec=spec)
        except Exception:
            yield ChatEvent("token", "I couldn't put a good quiz together on that just now. "
                                     "Try a narrower topic, or ask me again in a moment.")
            return

        title = f"{topic[:1].upper()}{topic[1:]} quiz"
        yield ChatEvent("artifact", ChatArtifact(
            conversation_id=ctx.conversation.id, kind="quiz", title=title,
            payload=quiz.model_dump(mode="json"),
            meta={"slots": _jsonable(slots)}))
        yield ChatEvent("token", _lead_in(topic, len(quiz.questions), difficulty))

    def fill_slot(self, slot: str, text: str) -> str:
        return re.sub(r"^\s*(?:on|about|for)\s+", "", text.strip(), flags=re.IGNORECASE)


__all__ = ["SPEC", "QuizSkill", "RoutedIntent", "build_spec", "coerce_slots"]
