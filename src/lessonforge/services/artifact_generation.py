"""Targeted-artifact generation — a generic, registry-driven seam.

Where :class:`~lessonforge.services.generation.LessonGenerator` builds a whole
lesson, an :class:`ArtifactGenerator` builds *one* artifact a teacher asked for on
its own — a quiz, a worksheet, a deck — without fabricating a full lesson to throw
away. It reuses the same machinery as the lesson path:

- the :class:`~lessonforge.domain.ldd.NormalizedBrief` produced by intake,
- the :class:`~lessonforge.rag.grounding.GroundingRetriever` (so a targeted
  artifact is grounded in real course material, incl. the ``reference`` book, and
  is framework/grade/subject aware),
- the shared model→domain boundary
  (:func:`~lessonforge.services.model_assembler.assemble_model`).

Generators self-register by :class:`ArtifactKind`, mirroring the renderer / loader
/ provider registries. **A new targeted artifact is a new subclass + one
``@register_generator`` decorator — no change to the pipeline, API, or DI.**
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from ..domain.artifacts import Quiz, Slides, Worksheet
from ..domain.ldd import NormalizedBrief
from ..export.base import ArtifactKind
from ..providers.base import LLMClient
from ..rag.grounding import GroundingBundle, GroundingRetriever, ensure_sources
from .model_assembler import assemble_model
from .normalize import normalize_artifact

# (data) -> (normalized, notes); reused as the assembler's normalizer.
Normalizer = Callable[[Any], "tuple[Any, list[str]]"]


class ArtifactGenerator(ABC):
    """Base for every targeted-artifact generator. Subclasses declare their
    :class:`ArtifactKind`, their IR ``model``, a system prompt, a few-shot example,
    and the per-artifact hard requirements; the shared :meth:`generate` does the
    grounding, prompting, assembly, and provenance stamping."""

    kind: ArtifactKind
    model: type[BaseModel]
    normalizer: Normalizer | None = staticmethod(normalize_artifact)

    def __init__(
        self,
        *,
        llm: LLMClient,
        grounding: GroundingRetriever | None = None,
        max_repairs: int = 1,
    ) -> None:
        self.llm = llm
        self.grounding = grounding
        self.max_repairs = max_repairs

    # ── subclass hooks ───────────────────────────────────────────────────────
    @property
    @abstractmethod
    def _system(self) -> str:
        """The role/system prompt."""

    @abstractmethod
    def _example(self, brief: NormalizedBrief) -> dict[str, Any]:
        """A concrete, structurally-valid example of the IR to anchor the model."""

    @abstractmethod
    def _requirements(self) -> str:
        """The hard requirements block appended to the prompt."""

    # ── the shared pipeline ──────────────────────────────────────────────────
    def _ground(self, brief: NormalizedBrief) -> GroundingBundle:
        if self.grounding is None:
            return GroundingBundle()
        try:
            return self.grounding.ground(
                query=f"{brief.topic} grade {brief.grade} {brief.subject}",
                grade=brief.grade,
                subject=brief.subject,
                framework=brief.framework,
            )
        except Exception:
            return GroundingBundle()

    def _prompt(self, brief: NormalizedBrief, grounding_ctx: str) -> str:
        return _PROMPT_TEMPLATE.format(
            kind=self.kind.value.replace("_", " "),
            grade=brief.grade,
            subject=brief.subject,
            topic=brief.topic,
            language=brief.language,
            grounding=grounding_ctx,
            example=json.dumps(self._example(brief), ensure_ascii=False, indent=2),
            requirements=self._requirements().strip(),
        )

    def generate(self, brief: NormalizedBrief) -> BaseModel:
        bundle = self._ground(brief)
        prompt = self._prompt(brief, bundle.as_prompt_context())
        result = self.llm.complete(
            prompt, system=self._system, json_schema=self.model.model_json_schema()
        )
        outcome = assemble_model(
            result.text,
            model=self.model,
            llm=self.llm,
            normalizer=self.normalizer,
            max_repairs=self.max_repairs,
        )
        if not outcome.ok or outcome.obj is None:
            raise ValueError(
                f"could not generate a valid {self.kind.value}: " + "; ".join(outcome.errors)
            )
        obj = outcome.obj
        # provenance is authoritative AND mandatory: stamp the sources we actually
        # retrieved, or the honest "model general knowledge" marker when there were
        # none — so every generated artifact quotes a source.
        if hasattr(obj, "grounding_sources"):
            obj.grounding_sources = ensure_sources(bundle.sources)
        return obj


_PROMPT_TEMPLATE = """\
Create a {kind} for Grade {grade} {subject}.
Topic: {topic}
Language mode: {language} (English body; Nepali terms where natural).

{grounding}

Return a single JSON object with EXACTLY the same keys and nesting as this example
(replace the content, keep the structure):

{example}

{requirements}
Return JSON only, no prose, no markdown fences.
"""


# ── registry ─────────────────────────────────────────────────────────────────
GENERATOR_REGISTRY: dict[ArtifactKind, type[ArtifactGenerator]] = {}


def register_generator(kind: ArtifactKind):
    def deco(cls: type[ArtifactGenerator]) -> type[ArtifactGenerator]:
        if kind in GENERATOR_REGISTRY:
            raise ValueError(f"generator for {kind} already registered as {GENERATOR_REGISTRY[kind]!r}")
        cls.kind = kind
        GENERATOR_REGISTRY[kind] = cls
        return cls

    return deco


def generatable_kinds() -> list[ArtifactKind]:
    return sorted(GENERATOR_REGISTRY, key=lambda k: k.value)


def build_generator(
    kind: ArtifactKind,
    *,
    llm: LLMClient,
    grounding: GroundingRetriever | None = None,
    max_repairs: int = 1,
) -> ArtifactGenerator:
    try:
        cls = GENERATOR_REGISTRY[kind]
    except KeyError:
        available = ", ".join(k.value for k in generatable_kinds()) or "<none>"
        raise ValueError(
            f"No generator for {kind.value!r}. Generatable: {available}."
        ) from None
    return cls(llm=llm, grounding=grounding, max_repairs=max_repairs)


def _ref(brief: NormalizedBrief) -> dict[str, Any]:
    return {"board": "CDC", "grade": brief.grade, "subject": brief.subject, "code": None}


# ── concrete generators ──────────────────────────────────────────────────────
@register_generator(ArtifactKind.quiz)
class QuizGenerator(ArtifactGenerator):
    model = Quiz

    @property
    def _system(self) -> str:
        return (
            "You are an experienced Nepali secondary-school teacher writing a short "
            "assessment. You write clear, unambiguous questions at the right grade "
            "level, mix question types, and always provide the correct answer. You "
            "output ONLY valid JSON matching the provided schema."
        )

    def _example(self, brief: NormalizedBrief) -> dict[str, Any]:
        return {
            "topic": "Sound and Vibration",
            "curriculum_ref": {"board": "CDC", "grade": 7, "subject": "Science", "code": None},
            "objectives": [
                {"id": "O1", "statement": "Explain that sound is produced by vibration",
                 "bloom": "understand"}
            ],
            "questions": [
                {"id": "Q1", "type": "mcq", "prompt": "Which of these produces sound?",
                 "answer": "a vibrating string", "objective_ids": ["O1"],
                 "options": ["a still stone", "a vibrating string", "a cold glass", "a dry leaf"]},
                {"id": "Q2", "type": "true_false", "prompt": "Sound can travel through empty space.",
                 "answer": "False", "objective_ids": ["O1"], "options": None},
                {"id": "Q3", "type": "short_answer",
                 "prompt": "Name one instrument in your home that makes sound by vibrating.",
                 "answer": "madal drum", "objective_ids": ["O1"], "options": None},
            ],
            "instructions": "Answer all questions. For each MCQ, choose the best option.",
            "grounding_sources": [],
        }

    def _requirements(self) -> str:
        return """\
Hard requirements (the output is rejected otherwise):
- Provide 1–3 objectives, and EVERY question's objective_ids must reference a declared objective id.
- Include a mix of question types where sensible; give the correct `answer` for each.
- For "mcq" questions include an "options" list; otherwise set "options": null.
- Use local Nepali examples where they fit the topic."""


@register_generator(ArtifactKind.worksheet)
class WorksheetGenerator(ArtifactGenerator):
    model = Worksheet

    @property
    def _system(self) -> str:
        return (
            "You are an experienced Nepali secondary-school teacher writing a student "
            "worksheet: hands-on tasks that build understanding, then practice "
            "questions with answers. You output ONLY valid JSON matching the schema."
        )

    def _example(self, brief: NormalizedBrief) -> dict[str, Any]:
        return {
            "topic": "Sound and Vibration",
            "curriculum_ref": {"board": "CDC", "grade": 7, "subject": "Science", "code": None},
            "objectives": [
                {"id": "O1", "statement": "Explain that sound is produced by vibration",
                 "bloom": "understand"}
            ],
            "tasks": [
                "Place your hand on your throat and hum. Write what you feel.",
                "Pluck a stretched rubber band and describe what you see and hear.",
            ],
            "questions": [
                {"id": "Q1", "type": "short_answer", "prompt": "What causes sound?",
                 "answer": "vibration", "objective_ids": ["O1"], "options": None},
                {"id": "Q2", "type": "true_false", "prompt": "A drum makes sound when its skin vibrates.",
                 "answer": "True", "objective_ids": ["O1"], "options": None},
            ],
            "grounding_sources": [],
        }

    def _requirements(self) -> str:
        return """\
Hard requirements (the output is rejected otherwise):
- Provide 1–3 objectives; include at least one hands-on task AND at least one question.
- EVERY question's objective_ids must reference a declared objective id; give each an `answer`.
- For "mcq" questions include an "options" list; otherwise set "options": null.
- Tasks should be doable with everyday materials found in a Nepali home or classroom."""


@register_generator(ArtifactKind.slides)
class SlidesGenerator(ArtifactGenerator):
    model = Slides

    @property
    def _system(self) -> str:
        return (
            "You are an experienced Nepali secondary-school teacher building a short "
            "classroom slide deck. You open on curiosity (a hook), not a definition, "
            "then teach in a clear sequence, and close with a check. You output ONLY "
            "valid JSON matching the schema."
        )

    def _example(self, brief: NormalizedBrief) -> dict[str, Any]:
        return {
            "topic": "Sound and Vibration",
            "curriculum_ref": {"board": "CDC", "grade": 7, "subject": "Science", "code": None},
            "subtitle": "CDC · Grade 7 · Science",
            "slides": [
                {"heading": "Let's begin…", "subtitle": "(demonstration)",
                 "bullets": ["Place your hand on your throat and hum — what do you feel?"]},
                {"heading": "What we'll be able to do", "subtitle": None,
                 "bullets": ["Explain that sound is produced by vibration"]},
                {"heading": "Sound is vibration", "subtitle": None,
                 "bullets": ["A madal drum's skin shakes", "A plucked string shakes",
                             "No vibration → no sound"]},
                {"heading": "Check for Understanding", "subtitle": None,
                 "bullets": ["Name one vibrating object that makes sound at home."]},
            ],
            "grounding_sources": [],
        }

    def _requirements(self) -> str:
        return """\
Hard requirements (the output is rejected otherwise):
- The FIRST slide must be a curiosity hook (a scenario/question/demonstration), not a definition.
- Include an objectives slide early, 2–4 teaching slides, and a closing check slide.
- Keep bullets short (a phrase, not a paragraph); use local Nepali examples where they fit."""
