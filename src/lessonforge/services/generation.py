"""LessonGenerator — the enrichment stage (brief → validated LDD).

M1 vertical slice: retrieve grounding context, prompt the LLM for a structured
LDD constrained to the LDD JSON schema, validate it, and return it. The critique
loop, per-artifact generation, and renderers layer on top of this without
changing the interface.
"""

from __future__ import annotations

import json
from typing import Any

from ..domain.ldd import LessonDesignDocument, NormalizedBrief
from ..providers.base import LLMClient
from ..rag.retriever import Retriever
from ..util import extract_json

# A concrete, minimal, structurally-valid example. Cloud models that ignore
# Ollama's `format` schema constraint still follow a shown example closely, so
# few-shot shape-anchoring is what actually makes structured output reliable.
_EXAMPLE_LDD: dict[str, Any] = {
    "topic": "Sound and Vibration",
    "curriculum_ref": {"board": "CDC", "grade": 7, "subject": "Science", "code": None},
    "duration_min": 45,
    "language": "en-ne",
    "framework": "5E",
    "objectives": [
        {"id": "O1", "statement": "Explain that sound is produced by vibration",
         "bloom": "understand"}
    ],
    "prior_knowledge": ["Objects can move and shake"],
    "misconceptions": [
        {"statement": "Sound needs no medium to travel",
         "correction": "Sound needs a medium such as air or water", "source": None}
    ],
    "engagement_hook": {
        "prompt": "Place your hand on your throat and hum — what do you feel?",
        "kind": "demonstration",
    },
    "local_context": ["temple bell", "madal drum", "flowing khola"],
    "phases": [
        {"name_en": "Engage", "name_ne": "संलग्न गराउनु",
         "teacher_activities": ["Ring a temple bell and ask what makes the sound"],
         "student_activities": ["Feel the vibration of the bell"],
         "minutes": 8, "objective_ids": ["O1"]}
    ],
    "materials": ["A small bell", "Rubber band"],
    "formative_checks": [
        {"id": "Q1", "type": "short_answer",
         "prompt": "What causes sound?", "answer": "Vibration", "objective_ids": ["O1"],
         "options": None}
    ],
    "homework": {"instructions": ["List three vibrating objects at home"],
                 "objective_ids": ["O1"]},
}

_SYSTEM = (
    "You are an experienced Nepali secondary-school teacher and curriculum "
    "designer. You design lessons the way a 20-year veteran does: open with a "
    "curiosity hook grounded in students' local world (paddy fields, goats, "
    "rivers, monsoon), surface the misconceptions common at this grade, and "
    "build active learning before any definition. You align every objective to "
    "both an activity and an assessment. You output ONLY valid JSON matching the "
    "provided schema."
)

_PROMPT_TEMPLATE = """\
Design a {duration}-minute {framework} lesson for Grade {grade} {subject}.
Topic: {topic}
Language mode: {language} (English body, Nepali pedagogical phase labels).

{grounding}

Return a single JSON object with EXACTLY the same keys and nesting as this
example (replace the content, keep the structure). Use "id" strings like
"O1","O2" for objectives and reference them in each phase's `objective_ids`
and each question's `objective_ids`:

{example}

Hard requirements (the output is rejected otherwise):
- engagement_hook.prompt must be a scenario/question/demonstration, never a definition.
- At least one grade-specific misconception with its correction.
- EVERY objective id must appear in at least one phase's objective_ids AND at
  least one formative_checks question's objective_ids.
- Use local Nepali examples in local_context and throughout the activities.
- For "mcq" questions include an "options" list; otherwise set "options": null.
Return JSON only, no prose, no markdown fences.
"""


class LessonGenerator:
    def __init__(self, *, llm: LLMClient, retriever: Retriever | None = None) -> None:
        self.llm = llm
        self.retriever = retriever

    def _grounding(self, brief: NormalizedBrief) -> str:
        if self.retriever is None:
            return "No retrieved context available; rely on curriculum knowledge."
        try:
            chunks = self.retriever.retrieve(
                collection="pedagogical",
                query=f"{brief.topic} grade {brief.grade} {brief.subject}",
            )
        except Exception:
            return "No retrieved context available; rely on curriculum knowledge."
        if not chunks:
            return "No retrieved context available; rely on curriculum knowledge."
        joined = "\n".join(f"- {c.text}" for c in chunks)
        return f"Grounding context (use it, cite sources in quality.grounding_sources):\n{joined}"

    def generate(self, brief: NormalizedBrief) -> LessonDesignDocument:
        schema: dict[str, Any] = LessonDesignDocument.model_json_schema()
        prompt = _PROMPT_TEMPLATE.format(
            duration=brief.duration_min,
            framework=brief.framework,
            grade=brief.grade,
            subject=brief.subject,
            topic=brief.topic,
            language=brief.language,
            grounding=self._grounding(brief),
            example=json.dumps(_EXAMPLE_LDD, ensure_ascii=False, indent=2),
        )
        result = self.llm.complete(prompt, system=_SYSTEM, json_schema=schema)
        try:
            data = extract_json(result.text)
        except ValueError as exc:
            raise ValueError(f"LLM did not return valid JSON: {exc}") from exc
        # Pydantic validation IS the anti-generic guardrail — a structurally
        # deficient lesson raises here rather than reaching the user.
        return LessonDesignDocument.model_validate(data)
