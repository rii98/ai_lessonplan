"""The assessment blueprint — what a teacher asks a quiz/worksheet to contain — and
the pure logic that turns a flat list of questions into a printable layout.

Three things live here, deliberately free of any LLM / export / API dependency so
the generator, the renderers, and the UI catalogue all read ONE definition:

- :data:`QUESTION_TYPES` — one row per question type: its label, the student-facing
  instruction, default marks, which *retrieval profile* grounds it, the rule the
  prompt gives the model, and a worked example. **Adding a question type is one
  enum member plus one row here** (and, if its layout is new, one branch in
  :func:`view`); the prompt, retrieval plan, UI, and every renderer follow.
- :class:`AssessmentSpec` — the author's blueprint: per type a count, a difficulty,
  optional marks and an optional instruction for the AI.
- :func:`build_sections` / :func:`view` — the shared layout. Matching columns and
  ordering steps are *shuffled deterministically* (seeded by the question) so the
  sheet and its answer key always agree, byte-for-byte, across runs and formats.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from .ldd import Question, QuestionType

MAX_PER_TYPE = 25
MAX_TOTAL = 60

DifficultyChoice = Literal["easy", "medium", "hard", "mixed"]
Scope = Literal["auto", "focused", "section", "chapter"]
# How a type is grounded — see ``services.assessment_retrieval``.
RetrievalProfile = Literal["facts", "explain", "worked"]


# ── the type catalogue ───────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class TypeMeta:
    label: str                 # picker label
    section_title: str         # printed section heading
    student_instruction: str   # printed under the heading
    hint: str                  # one-line help in the builder
    default_marks: int
    default_count: int
    retrieval: RetrievalProfile
    rule: str                  # what the prompt tells the model about this type
    example: dict[str, Any]    # a structurally valid example question


_O = ["O1"]

QUESTION_TYPES: dict[QuestionType, TypeMeta] = {
    QuestionType.mcq: TypeMeta(
        label="Multiple choice", section_title="Multiple Choice Questions",
        student_instruction="Choose the best answer for each question.",
        hint="4 options, one correct. Distractors come from common misconceptions.",
        default_marks=1, default_count=5, retrieval="facts",
        rule=("exactly 4 `options`, exactly one correct; `answer` equals one option verbatim; "
              "distractors are plausible (use real misconceptions); never 'all of the above'."),
        example={"id": "Q1", "type": "mcq", "prompt": "Which of these produces sound?",
                 "options": ["a still stone", "a vibrating string", "a cold glass", "a dry leaf"],
                 "answer": "a vibrating string", "objective_ids": _O, "difficulty": "easy"},
    ),
    QuestionType.true_false: TypeMeta(
        label="True / False", section_title="True or False",
        student_instruction="Write True or False for each statement.",
        hint="One unambiguous statement each. Balanced True/False.",
        default_marks=1, default_count=5, retrieval="facts",
        rule=("one declarative statement that is clearly true or clearly false; `answer` is "
              "'True' or 'False'; roughly balance the two; no 'always/never' giveaways."),
        example={"id": "Q2", "type": "true_false",
                 "prompt": "Sound can travel through empty space.", "answer": "False",
                 "objective_ids": _O, "difficulty": "easy"},
    ),
    QuestionType.fill_blank: TypeMeta(
        label="Fill in the blanks", section_title="Fill in the Blanks",
        student_instruction="Fill in each blank with the correct word or term.",
        hint="A sentence with a key term removed.",
        default_marks=1, default_count=5, retrieval="facts",
        rule=("a sentence containing ONE blank written exactly as '_____'; the blank removes a "
              "key concept (never a trivial word); `answer` is the missing term only."),
        example={"id": "Q3", "type": "fill_blank",
                 "prompt": "Sound is produced when an object _____.", "answer": "vibrates",
                 "objective_ids": _O, "difficulty": "easy"},
    ),
    QuestionType.matching: TypeMeta(
        label="Match the following", section_title="Match the Following",
        student_instruction="Match each item in Column A with its partner in Column B.",
        hint="Each question is a set of 4–6 pairs (so '1' = one matching set).",
        default_marks=4, default_count=1, retrieval="facts",
        rule=("`pairs` is 4–6 {left, right} items; every left matches exactly one right; the "
              "`prompt` is a short instruction; leave `answer` empty and `options` null (the "
              "answer key is computed)."),
        example={"id": "Q4", "type": "matching", "prompt": "Match the instrument to what vibrates.",
                 "pairs": [{"left": "Madal", "right": "stretched skin"},
                           {"left": "Sarangi", "right": "bowed string"},
                           {"left": "Flute", "right": "column of air"},
                           {"left": "Cymbal", "right": "metal plate"}],
                 "answer": "", "objective_ids": _O, "difficulty": "medium"},
    ),
    QuestionType.short_answer: TypeMeta(
        label="Short answer", section_title="Short Answer Questions",
        student_instruction="Answer in one to three sentences.",
        hint="Answerable in 1–3 sentences.",
        default_marks=2, default_count=3, retrieval="facts",
        rule=("answerable in 1–3 sentences; `answer` is a concise model answer; add 1–3 "
              "`key_points` marking points."),
        example={"id": "Q5", "type": "short_answer",
                 "prompt": "Name one instrument in your home that makes sound by vibrating.",
                 "answer": "A madal: its stretched skin vibrates when struck.",
                 "key_points": ["names a valid instrument", "links the sound to vibration"],
                 "objective_ids": _O, "difficulty": "medium"},
    ),
    QuestionType.long_answer: TypeMeta(
        label="Explain / long answer", section_title="Long Answer Questions",
        student_instruction="Answer in detail. Give reasons and examples where you can.",
        hint="'Why / how / explain' questions with a marking scheme.",
        default_marks=5, default_count=2, retrieval="explain",
        rule=("asks the student to explain, describe, compare or justify (why/how), not to "
              "recall; `answer` is a model answer of 3–6 sentences; `key_points` lists 3–5 "
              "marking points a teacher can tick off."),
        example={"id": "Q6", "type": "long_answer",
                 "prompt": "Explain why we cannot hear sound in space.",
                 "answer": ("Sound needs a medium such as air to carry the vibrations. Space is "
                            "almost empty, so there is nothing to carry the vibrations to our "
                            "ears."),
                 "key_points": ["sound is a vibration that needs a medium",
                                "space is (almost) a vacuum", "so no sound reaches the ear"],
                 "objective_ids": _O, "difficulty": "hard"},
    ),
    QuestionType.numerical: TypeMeta(
        label="Numerical / problem", section_title="Numerical Problems",
        student_instruction="Solve. Show your working and write the unit with your answer.",
        hint="A worked problem with numbers, from a local context.",
        default_marks=4, default_count=2, retrieval="worked",
        rule=("a problem with concrete numbers set in a realistic Nepali context; `answer` "
              "states the final result WITH its unit; `key_points` are the solution steps in "
              "order; the numbers must make the problem solvable from the material."),
        example={"id": "Q7", "type": "numerical",
                 "prompt": ("A bus travels 120 km from Kathmandu to Pokhara in 4 hours. "
                            "Find its average speed."),
                 "answer": "30 km/h", "key_points": ["speed = distance ÷ time",
                                                     "120 ÷ 4 = 30", "unit: km/h"],
                 "objective_ids": _O, "difficulty": "medium"},
    ),
    QuestionType.ordering: TypeMeta(
        label="Arrange in order", section_title="Arrange in the Correct Order",
        student_instruction="Arrange the steps in the correct order. Write the letters in order.",
        hint="A process or sequence — shuffled automatically.",
        default_marks=3, default_count=1, retrieval="explain",
        rule=("`options` is 4–6 steps/events listed in the CORRECT order (the sheet scrambles "
              "them for students); `prompt` says what to arrange; leave `answer` empty."),
        example={"id": "Q8", "type": "ordering",
                 "prompt": "Arrange the steps by which we hear a sound.",
                 "options": ["An object vibrates", "The air around it vibrates",
                             "The vibration reaches the ear", "The brain recognises the sound"],
                 "answer": "", "objective_ids": _O, "difficulty": "medium"},
    ),
}


def meta(t: QuestionType) -> TypeMeta:
    return QUESTION_TYPES[t]


# ── the author's blueprint ───────────────────────────────────────────────────
class TypeSpec(BaseModel):
    """One row of the blueprint: ``count`` questions of ``type`` at ``difficulty``."""

    type: QuestionType
    count: int = Field(ge=1, le=MAX_PER_TYPE)
    difficulty: DifficultyChoice = "mixed"
    marks_each: int | None = Field(default=None, ge=1, le=50)
    # The author's free-text steer for THIS type ("use diagrams from the lab",
    # "keep the numbers whole"). Refines content/style only — never the schema.
    instruction: str = Field(default="", max_length=500)


class AssessmentSpec(BaseModel):
    """What the author wants: a list of per-type rows plus a retrieval scope."""

    types: list[TypeSpec] = Field(min_length=1)
    # auto: pick retrieval per type (recommended). Otherwise force one granularity.
    scope: Scope = "auto"
    # Ask the model for a student-facing ``explanation`` on every question (the
    # feedback an interactive quiz shows after each answer). Off for printed sheets.
    explanations: bool = False

    @model_validator(mode="after")
    def _valid(self) -> AssessmentSpec:
        seen = [t.type for t in self.types]
        dup = {t for t in seen if seen.count(t) > 1}
        if dup:
            raise ValueError(f"each question type may appear once; repeated: {sorted(d.value for d in dup)}")
        if self.total_questions > MAX_TOTAL:
            raise ValueError(f"too many questions ({self.total_questions}); the limit is {MAX_TOTAL}")
        return self

    @property
    def total_questions(self) -> int:
        return sum(t.count for t in self.types)

    @property
    def total_marks(self) -> int:
        return sum(t.count * (t.marks_each or 0) for t in self.types)

    def for_type(self, t: QuestionType) -> TypeSpec | None:
        return next((s for s in self.types if s.type is t), None)


PRESETS: list[dict[str, Any]] = [
    {"id": "quick", "label": "Quick check",
     "types": [{"type": "mcq", "count": 5, "difficulty": "easy", "marks_each": 1},
               {"type": "true_false", "count": 3, "difficulty": "easy", "marks_each": 1}]},
    {"id": "unit_test", "label": "Unit test",
     "types": [{"type": "mcq", "count": 6, "difficulty": "mixed", "marks_each": 1},
               {"type": "fill_blank", "count": 4, "difficulty": "easy", "marks_each": 1},
               {"type": "matching", "count": 1, "difficulty": "medium", "marks_each": 4},
               {"type": "short_answer", "count": 3, "difficulty": "medium", "marks_each": 2},
               {"type": "long_answer", "count": 1, "difficulty": "hard", "marks_each": 5}]},
    {"id": "exam", "label": "Exam paper",
     "types": [{"type": "mcq", "count": 10, "difficulty": "mixed", "marks_each": 1},
               {"type": "true_false", "count": 5, "difficulty": "easy", "marks_each": 1},
               {"type": "short_answer", "count": 5, "difficulty": "medium", "marks_each": 2},
               {"type": "long_answer", "count": 3, "difficulty": "hard", "marks_each": 5},
               {"type": "numerical", "count": 2, "difficulty": "hard", "marks_each": 4}]},
]


# ── deterministic shuffling ──────────────────────────────────────────────────
def _letter(i: int) -> str:
    return chr(ord("A") + i) if i < 26 else str(i + 1)


def _shuffled(n: int, seed: str) -> list[int]:
    """A permutation of ``range(n)`` seeded by ``seed`` — stable across runs and
    processes (``random.Random(str)`` hashes with SHA-512, not ``hash()``), and
    never the identity for ``n >= 2`` so the sheet never hands students the key."""
    order = list(range(n))
    random.Random(seed).shuffle(order)
    if n >= 2 and order == list(range(n)):
        order = order[1:] + order[:1]
    return order


# ── per-question view ────────────────────────────────────────────────────────
@dataclass(slots=True)
class QuestionView:
    """Everything a renderer needs for one question, format-agnostic."""

    number: int
    type: QuestionType
    prompt: str
    marks: int | None
    difficulty: str | None
    choices: list[str] = field(default_factory=list)  # "A. …" lines (mcq / ordering)
    left: list[str] = field(default_factory=list)     # "1. …" (matching column A)
    right: list[str] = field(default_factory=list)    # "A. …" (matching column B, shuffled)
    true_false: bool = False
    response_lines: int = 0                           # ruled lines to write on
    answer: str = ""                                  # the answer-key text
    key_points: list[str] = field(default_factory=list)
    # The key as letters, for machine grading: matching → the letter of each left
    # item's partner (in left order); ordering → the letters in correct order.
    answer_letters: list[str] = field(default_factory=list)


# Ruled lines under a written-answer question. The legacy flat layout keeps one
# "Answer: ____" line for a short answer (byte-stable with earlier exports); the
# sectioned layout is ``roomy`` and gives it real writing space.
_RESPONSE_LINES = {
    QuestionType.fill_blank: 0,   # the blank is in the prompt itself
    QuestionType.short_answer: 1,
    QuestionType.long_answer: 6,
    QuestionType.numerical: 5,
}
_ROOMY_SHORT_ANSWER_LINES = 3


def view(q: Question, number: int, *, default_marks: int | None = None,
         roomy: bool = False) -> QuestionView:
    v = QuestionView(
        number=number, type=q.type, prompt=q.prompt,
        marks=q.marks if q.marks is not None else default_marks,
        difficulty=q.difficulty.value if q.difficulty else None,
        answer=q.answer, key_points=list(q.key_points),
    )
    t = q.type
    if t is QuestionType.mcq:
        v.choices = [f"{_letter(j)}. {o}" for j, o in enumerate(q.options or [])]
    elif t is QuestionType.true_false:
        v.true_false = True
    elif t is QuestionType.matching:
        pairs = q.pairs or []
        order = _shuffled(len(pairs), f"{q.id}|{q.prompt}")  # position j shows pair order[j]'s right
        v.left = [f"{i + 1}. {p.left}" for i, p in enumerate(pairs)]
        v.right = [f"{_letter(j)}. {pairs[k].right}" for j, k in enumerate(order)]
        letter_of = {k: _letter(j) for j, k in enumerate(order)}
        v.answer = ",  ".join(f"{i + 1}–{letter_of[i]}" for i in range(len(pairs)))
        v.answer_letters = [letter_of[i] for i in range(len(pairs))]
    elif t is QuestionType.ordering:
        steps = q.options or []
        order = _shuffled(len(steps), f"{q.id}|{q.prompt}")
        v.choices = [f"{_letter(j)}. {steps[k]}" for j, k in enumerate(order)]
        letter_of = {k: _letter(j) for j, k in enumerate(order)}
        v.answer = " → ".join(letter_of[i] for i in range(len(steps)))
        v.answer_letters = [letter_of[i] for i in range(len(steps))]
        v.response_lines = 1
    else:
        v.response_lines = _RESPONSE_LINES.get(t, 1)
        if roomy and t is QuestionType.short_answer:
            v.response_lines = _ROOMY_SHORT_ANSWER_LINES
    return v


# ── sectioned layout ─────────────────────────────────────────────────────────
@dataclass(slots=True)
class Section:
    letter: str
    type: QuestionType
    title: str
    instruction: str
    marks_note: str            # "5 × 1 = 5 marks" / "10 marks" / "" when unmarked
    items: list[QuestionView]


def _marks_note(views: list[QuestionView]) -> str:
    marks = [v.marks or 0 for v in views]
    total = sum(marks)
    if not total:
        return ""
    unit = "mark" if total == 1 else "marks"
    if len(set(marks)) == 1:
        return f"{len(views)} × {marks[0]} = {total} {unit}"
    return f"{total} {unit}"


def build_sections(
    questions: list[Question], spec: AssessmentSpec | None
) -> list[Section] | None:
    """Group ``questions`` into printable sections, in the blueprint's order.

    Returns ``None`` when there is no blueprint — callers then render the legacy
    flat numbered list, so a quiz projected from a lesson looks exactly as before.
    Numbering runs continuously across sections (so the answer key is one list). A
    type that is present in the questions but not in the blueprint (a question the
    teacher added by hand) is appended as its own section rather than dropped."""
    if spec is None:
        return None
    order = [s.type for s in spec.types]
    order += [t for t in dict.fromkeys(q.type for q in questions) if t not in order]
    sections: list[Section] = []
    n = 0
    for t in order:
        qs = [q for q in questions if q.type is t]
        if not qs:
            continue
        row = spec.for_type(t)
        default_marks = row.marks_each if row else None
        items = []
        for q in qs:
            n += 1
            items.append(view(q, n, default_marks=default_marks, roomy=True))
        m = meta(t)
        sections.append(Section(
            letter=_letter(len(sections)), type=t, title=m.section_title,
            instruction=m.student_instruction, marks_note=_marks_note(items), items=items,
        ))
    return sections


def flat_views(questions: list[Question]) -> list[QuestionView]:
    """The legacy, unsectioned numbering (no marks, no headings)."""
    return [view(q, i) for i, q in enumerate(questions, 1)]


def full_marks(sections: list[Section] | None) -> int:
    return sum((v.marks or 0) for s in sections or [] for v in s.items)
