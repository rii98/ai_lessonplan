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
from ..domain.assessment import QUESTION_TYPES, AssessmentSpec, TypeSpec
from ..domain.ldd import Difficulty, NormalizedBrief, Question
from ..export.base import ArtifactKind
from ..providers.base import LLMClient
from ..rag.grounding import GroundingBundle, GroundingRetriever, ensure_sources
from ..util import extract_json
from .assessment_retrieval import AssessmentGrounder
from .model_assembler import assemble_model
from .normalize import normalize_artifact, normalize_question

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
    # True for generators that honour an :class:`AssessmentSpec` blueprint.
    supports_spec: bool = False

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
    def _ground(self, brief: NormalizedBrief, spec: AssessmentSpec | None = None) -> GroundingBundle:
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

    def _prompt(
        self, brief: NormalizedBrief, grounding_ctx: str, spec: AssessmentSpec | None = None
    ) -> str:
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

    def generate(self, brief: NormalizedBrief, spec: AssessmentSpec | None = None) -> BaseModel:
        if spec is not None and not self.supports_spec:
            raise ValueError(f"a {self.kind.value} has no question blueprint to apply")
        bundle = self._ground(brief, spec)
        grounding_ctx = bundle.as_prompt_context()
        prompt = self._prompt(brief, grounding_ctx, spec)
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
        return self._finalize(obj, brief=brief, grounding_ctx=grounding_ctx, spec=spec)

    def _finalize(
        self, obj: BaseModel, *, brief: NormalizedBrief, grounding_ctx: str,
        spec: AssessmentSpec | None,
    ) -> BaseModel:
        """Post-generation hook: enforce anything the prompt could only *ask* for."""
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



# ── assessments: blueprint-driven quiz / worksheet ───────────────────────────
_DIFFICULTY_GUIDE = (
    "Difficulty scale — easy: recall or recognise something the material states outright; "
    "medium: explain or apply it in a familiar-but-new situation; hard: analyse, combine "
    "several ideas, compare or justify. 'mixed' means roughly 30% easy, 50% medium, 20% hard."
)

_ASSESSMENT_PROMPT = """\
Create a {kind} for Grade {grade} {subject}.
Topic: {topic}
Language mode: {language} (English body; Nepali terms where natural).

{grounding}

BLUEPRINT — write EXACTLY these questions, no more and no fewer:
{blueprint}

{difficulty_guide}
The author's per-type instructions above refine content and style only; they never change
the required counts or the JSON structure. Ground every question in the material above
when it is given; do not test facts the material does not support.

Rules per question type:
{type_rules}

Return a single JSON object with EXACTLY the same keys and nesting as this example
(replace the content, keep the structure; the example shows one question per requested
type — you must write the full counts above):

{example}

{requirements}
- Set "difficulty" on every question to "easy", "medium" or "hard" (never "mixed").
- Give every question's `objective_ids` a declared objective id.
Return JSON only, no prose, no markdown fences.
"""

_TOP_UP_PROMPT = """\
You are completing a {kind} on "{topic}" (Grade {grade} {subject}). These questions are
still MISSING — write exactly these, as NEW questions that do not repeat existing ones:
{blueprint}

{difficulty_guide}
Existing question prompts (do not repeat):
{existing}

Declared objective ids you may reference: {objective_ids}

{grounding}

Rules per question type:
{type_rules}

Return JSON only: {{"questions": [ ... ]}} — same question shape as this example:
{example}
"""


def _blueprint_lines(rows: list[TypeSpec]) -> str:
    out = []
    for r in rows:
        m = QUESTION_TYPES[r.type]
        line = f"- {r.count} × {r.type.value} ({m.label}), difficulty: {r.difficulty}"
        if r.marks_each:
            line += f", {r.marks_each} mark(s) each"
        if r.instruction.strip():
            line += f'\n    author instruction: "{r.instruction.strip()}"'
        out.append(line)
    return "\n".join(out)


def _type_rules(rows: list[TypeSpec]) -> str:
    return "\n".join(f"- {r.type.value}: {QUESTION_TYPES[r.type].rule}" for r in rows)


def _examples(rows: list[TypeSpec]) -> list[dict[str, Any]]:
    out = []
    for i, r in enumerate(rows, 1):
        q = dict(QUESTION_TYPES[r.type].example)
        q["id"] = f"Q{i}"
        out.append(q)
    return out


def _normalize_generated(data: Any):
    """Artifact normalizer for GENERATED output: also drops fields that are ours to
    stamp (``spec``, ``notes``) so a model that echoes or invents them can't fail
    validation on a field it was never asked to author."""
    out, notes = normalize_artifact(data)
    if isinstance(out, dict):
        out.pop("spec", None)
        out.pop("notes", None)
    return out, notes


class _AssessmentGenerator(ArtifactGenerator):
    """Shared engine for quiz and worksheet. Without a blueprint it behaves exactly
    like the plain generator; with one it (1) retrieves per question type, (2)
    prompts with the exact blueprint + per-type rules + examples of only the chosen
    types, then (3) *enforces* the counts the prompt can only request — trimming
    extras, topping up a shortfall once, stamping difficulty/marks, renumbering."""

    supports_spec = True
    normalizer = staticmethod(_normalize_generated)

    def _ground(self, brief, spec=None):
        if spec is None:
            return super()._ground(brief)
        return AssessmentGrounder(self.grounding).ground(brief, spec)

    def _prompt(self, brief, grounding_ctx, spec=None):
        if spec is None:
            return super()._prompt(brief, grounding_ctx)
        example = self._example(brief)
        example["questions"] = _examples(spec.types)
        # the generic options/pairs line names every type; the per-type rules replace it
        reqs = "\n".join(l for l in self._requirements().strip().splitlines()
                         if '"options" is for' not in l)
        return _ASSESSMENT_PROMPT.format(
            kind=self.kind.value, grade=brief.grade, subject=brief.subject,
            topic=brief.topic, language=brief.language, grounding=grounding_ctx,
            blueprint=_blueprint_lines(spec.types), difficulty_guide=_DIFFICULTY_GUIDE,
            type_rules=_type_rules(spec.types),
            example=json.dumps(example, ensure_ascii=False, indent=2),
            requirements=reqs,
        )

    def _finalize(self, obj, *, brief, grounding_ctx, spec):
        if spec is None:
            return obj
        notes: list[str] = []
        questions = _conform(list(obj.questions), spec)
        short = _shortfall(questions, spec)
        if short:
            questions = _conform(
                questions + self._top_up(obj, brief, grounding_ctx, short), spec
            )
            short = _shortfall(questions, spec)
        for row in short:  # row.count is the amount still missing
            asked = spec.for_type(row.type).count  # type: ignore[union-attr]
            notes.append(
                f"Wrote {asked - row.count} of {asked} requested "
                f"{QUESTION_TYPES[row.type].label.lower()} questions — add the rest by hand "
                f"or use ✨ Improve."
            )
        if not questions and not getattr(obj, "tasks", None):
            raise ValueError(
                f"could not generate a valid {self.kind.value}: the model returned no "
                "questions of the requested types"
            )
        obj.questions = questions
        obj.spec = spec
        obj.notes = notes
        return obj

    def _top_up(self, obj, brief, grounding_ctx, missing: list[TypeSpec]) -> list[Question]:
        """One bounded attempt to write only the missing questions. Any failure
        (non-JSON, an invalid question) just yields fewer — the shortfall is then
        reported honestly instead of failing the whole artifact."""
        prompt = _TOP_UP_PROMPT.format(
            kind=self.kind.value, topic=brief.topic, grade=brief.grade, subject=brief.subject,
            blueprint=_blueprint_lines(missing), difficulty_guide=_DIFFICULTY_GUIDE,
            existing="\n".join(f"- {q.prompt}" for q in obj.questions) or "- (none)",
            objective_ids=", ".join(o.id for o in obj.objectives),
            grounding=grounding_ctx, type_rules=_type_rules(missing),
            example=json.dumps({"questions": _examples(missing)}, ensure_ascii=False, indent=2),
        )
        try:
            raw = extract_json(self.llm.complete(prompt, system=self._system).text)
            items = raw.get("questions") if isinstance(raw, dict) else raw
        except Exception:
            return []
        valid_ids = {o.id for o in obj.objectives}
        parsed = [_parse_question(item, i) for i, item in
                  enumerate(items if isinstance(items, list) else [])]
        return [q for q in parsed if q is not None and set(q.objective_ids) <= valid_ids]


def _parse_question(item: Any, i: int) -> Question | None:
    """One model-written question → a valid :class:`Question`, or ``None``."""
    try:
        normalize_question(item, [], f"top_up[{i}]")
        return Question.model_validate(item)
    except Exception:
        return None


def _shortfall(questions: list[Question], spec: AssessmentSpec) -> list[TypeSpec]:
    """The rows still short of their requested count, trimmed to the missing amount."""
    out = []
    for row in spec.types:
        have = sum(1 for q in questions if q.type is row.type)
        if have < row.count:
            out.append(row.model_copy(update={"count": row.count - have}))
    return out


def _conform(questions: list[Question], spec: AssessmentSpec) -> list[Question]:
    """Hold ``questions`` to the blueprint: drop types that weren't asked for, trim
    each type to its count, order by blueprint, stamp the requested difficulty (a
    non-"mixed" request is authoritative) and default marks, and renumber Q1…Qn."""
    out: list[Question] = []
    for row in spec.types:
        for q in [q for q in questions if q.type is row.type][: row.count]:
            if row.difficulty != "mixed":
                q.difficulty = Difficulty(row.difficulty)
            if q.marks is None and row.marks_each:
                q.marks = row.marks_each
            out.append(q)
    for i, q in enumerate(out, 1):
        q.id = f"Q{i}"
    return out


# ── concrete generators ──────────────────────────────────────────────────────
@register_generator(ArtifactKind.quiz)
class QuizGenerator(_AssessmentGenerator):
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
- "options" is for "mcq" (the choices) and "ordering" (steps in correct order); "pairs" only for "matching"; otherwise null.
- Use local Nepali examples where they fit the topic."""


@register_generator(ArtifactKind.worksheet)
class WorksheetGenerator(_AssessmentGenerator):
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
- "options" is for "mcq" (the choices) and "ordering" (steps in correct order); "pairs" only for "matching"; otherwise null.
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
