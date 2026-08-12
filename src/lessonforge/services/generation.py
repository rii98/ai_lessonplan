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
from ..rag.grounding import GroundingBundle, GroundingRetriever
from .assemble import LDDAssembler

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
{personalization}{existing_plan}
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


def _personalization_block(brief: NormalizedBrief) -> str:
    """Inject the teacher's stored voice + favourite local anchors (from their
    TeacherProfile) so the lesson feels like *hers*, not generic."""
    parts: list[str] = []
    if brief.style_notes.strip():
        parts.append(f"Teaching voice to emulate: {brief.style_notes.strip()}")
    if brief.local_anchors:
        parts.append(
            "Prefer these local anchors the teacher's students recognize: "
            + ", ".join(brief.local_anchors)
        )
    if not parts:
        return ""
    return "This teacher's stored preferences (honor them):\n" + "\n".join(
        f"- {p}" for p in parts
    ) + "\n"


def _existing_plan_block(plan: str | None) -> str:
    """US-3: when the teacher pasted a draft, tell the model to enrich it — add
    the hook, misconceptions, and active learning it lacks — not discard it."""
    if not plan or not plan.strip():
        return ""
    return (
        "\nThe teacher pasted an existing draft below. ENRICH it — preserve their "
        "intent and any good content, and add what a veteran would (a curiosity "
        "hook, grade misconceptions, active-learning phases, aligned assessment). "
        "Do not simply replace it wholesale.\n"
        f"--- existing draft ---\n{plan.strip()}\n--- end draft ---\n"
    )


class LessonGenerator:
    def __init__(
        self,
        *,
        llm: LLMClient,
        grounding: GroundingRetriever | None = None,
        max_repairs: int = 1,
    ) -> None:
        self.llm = llm
        self.grounding = grounding
        # the shared model→domain boundary (normalize + bounded repair + degrade)
        self.assembler = LDDAssembler(llm=llm, max_repairs=max_repairs)

    def _ground(self, brief: NormalizedBrief) -> GroundingBundle:
        if self.grounding is None:
            return GroundingBundle()
        try:
            return self.grounding.ground(
                query=f"{brief.topic} grade {brief.grade} {brief.subject}",
                grade=brief.grade,
                subject=brief.subject,
            )
        except Exception:
            return GroundingBundle()

    def generate(self, brief: NormalizedBrief) -> LessonDesignDocument:
        bundle = self._ground(brief)
        schema: dict[str, Any] = LessonDesignDocument.model_json_schema()
        prompt = _PROMPT_TEMPLATE.format(
            duration=brief.duration_min,
            framework=brief.framework,
            grade=brief.grade,
            subject=brief.subject,
            topic=brief.topic,
            language=brief.language,
            personalization=_personalization_block(brief),
            existing_plan=_existing_plan_block(brief.existing_plan),
            grounding=bundle.as_prompt_context(),
            example=json.dumps(_EXAMPLE_LDD, ensure_ascii=False, indent=2),
        )
        result = self.llm.complete(prompt, system=_SYSTEM, json_schema=schema)
        # The boundary normalizes cosmetic deviations, validates against the
        # anti-generic guardrails, and repairs semantic ones via the model. A
        # lesson that still can't be made valid degrades to a clean error here
        # rather than a 500 or a structurally deficient plan reaching the user.
        outcome = self.assembler.assemble(result.text)
        if not outcome.ok or outcome.ldd is None:
            raise ValueError("could not generate a valid lesson: " + "; ".join(outcome.errors))
        ldd = outcome.ldd
        if outcome.notes:  # transparency trail (survives the critique stamp)
            ldd.quality.adjustments = outcome.notes
        # Provenance is authoritative: overwrite whatever the model claimed with
        # the sources we actually retrieved, so citations are trustworthy.
        if bundle.sources:
            ldd.quality.grounding_sources = bundle.sources
        return ldd
