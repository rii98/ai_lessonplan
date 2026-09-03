"""The reprompt-able surface of each artifact IR — the artifact counterpart of
:data:`~lessonforge.domain.sections.LDD_SECTIONS`.

Each map names the fields a teacher can improve (identity fields — topic,
curriculum_ref — are edited directly, never reprompted), the coupling the IR's own
validators tie them to (so a scoped edit can be repaired by a bounded cascade
instead of a whole rewrite), and whether the field is factual (``grounded``) so an
AI edit re-retrieves the reference material. Keyed by the artifact kind's string
value, so this module stays free of any export/service dependency.
"""

from __future__ import annotations

from pydantic import BaseModel

from .artifacts import Quiz, Slides, Worksheet
from .sections import Section

# Quiz / Worksheet: questions and objectives are mutually coupled through the
# `_questions_map_to_objectives` guardrail (a question must map to a real
# objective), so editing one may need the other repaired.
QUIZ_SECTIONS: dict[str, Section] = {
    "objectives": Section("objectives", coupled=("questions",)),
    "questions": Section("questions", coupled=("objectives",)),
    "instructions": Section("instructions", grounded=False),
}

WORKSHEET_SECTIONS: dict[str, Section] = {
    "objectives": Section("objectives", coupled=("questions",)),
    "tasks": Section("tasks"),
    "questions": Section("questions", coupled=("objectives",)),
}

SLIDES_SECTIONS: dict[str, Section] = {
    "slides": Section("slides"),
    "subtitle": Section("subtitle", grounded=False),
}

ARTIFACT_SECTIONS: dict[str, dict[str, Section]] = {
    "quiz": QUIZ_SECTIONS,
    "worksheet": WORKSHEET_SECTIONS,
    "slides": SLIDES_SECTIONS,
}

ARTIFACT_MODELS: dict[str, type[BaseModel]] = {
    "quiz": Quiz,
    "worksheet": Worksheet,
    "slides": Slides,
}
