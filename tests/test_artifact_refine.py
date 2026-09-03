"""ArtifactRefiner: scoped AI edits with two-layer validation, and the senior-call
re-grounding — content edits re-retrieve and MERGE sources; structural edits don't."""

from __future__ import annotations

import json
from typing import Any

from lessonforge.domain.artifact_sections import QUIZ_SECTIONS
from lessonforge.domain.artifacts import Quiz
from lessonforge.providers.base import LLMResult
from lessonforge.rag.grounding import GroundingBundle
from lessonforge.services.artifact_refine import ArtifactRefiner

_QUIZ = Quiz.model_validate({
    "topic": "Sound", "curriculum_ref": {"board": "CDC", "grade": 7, "subject": "Science"},
    "objectives": [{"id": "O1", "statement": "explain sound is a vibration", "bloom": "understand"}],
    "questions": [{"id": "Q1", "type": "short_answer", "prompt": "what causes sound?",
                   "answer": "vibration", "objective_ids": ["O1"]}],
    "instructions": "Answer all.",
    "grounding_sources": ["Old Book"],
})


class SectionLLM:
    """Returns a fixed {"section": ...} payload for every complete() call."""

    def __init__(self, section: Any) -> None:
        self._section = section

    def complete(self, prompt, *, system=None, json_schema=None, temperature=None) -> LLMResult:
        return LLMResult(text=json.dumps({"section": self._section}), raw={})

    def health(self) -> bool:
        return True


class SpyGrounding:
    def __init__(self, sources: list[str] | None = None) -> None:
        self.calls = 0
        self._sources = sources or []

    def ground(self, *, query, grade=None, subject=None, framework=None) -> GroundingBundle:
        self.calls += 1
        b = GroundingBundle()
        b.sources = list(self._sources)
        return b


def _refiner(llm, grounding=None):
    return ArtifactRefiner(kind="quiz", model=Quiz, sections=QUIZ_SECTIONS,
                           llm=llm, grounding=grounding)


def test_content_edit_regrounds_and_merges_sources():
    spy = SpyGrounding(sources=["My Science Grade 7"])
    new_qs = [{"id": "Q1", "type": "mcq", "prompt": "Which vibrates to make sound?",
               "answer": "a plucked string", "objective_ids": ["O1"],
               "options": ["a stone", "a plucked string", "cold water", "a dry leaf"]}]
    result = _refiner(SectionLLM(new_qs), spy).refine(_QUIZ, "questions", "make it an MCQ")
    assert result.ok
    assert result.candidate.questions[0].type.value == "mcq"
    assert spy.calls >= 1  # content target re-grounded
    # sources grew (union), the old one kept, the new one added
    assert "Old Book" in result.candidate.grounding_sources
    assert "My Science Grade 7" in result.candidate.grounding_sources


def test_structural_edit_makes_no_grounding_call():
    spy = SpyGrounding(sources=["Should not appear"])
    result = _refiner(SectionLLM("Read each question carefully."), spy).refine(
        _QUIZ, "instructions", "make the instructions clearer"
    )
    assert result.ok and result.candidate.instructions == "Read each question carefully."
    assert spy.calls == 0  # cosmetic target: no retrieval
    assert result.candidate.grounding_sources == ["Old Book"]  # carried across unchanged


def test_bad_fragment_degrades_to_reject():
    result = _refiner(SectionLLM("not a list of questions")).refine(
        _QUIZ, "questions", "improve them"
    )
    assert not result.ok and result.errors


def test_diff_is_scoped_to_the_edited_field():
    result = _refiner(SectionLLM("Answer neatly.")).refine(_QUIZ, "instructions", "x")
    assert set(result.diff) == {"instructions"}
    assert result.diff["instructions"]["after"] == "Answer neatly."


def test_unknown_target_raises():
    import pytest
    with pytest.raises(ValueError, match="unknown refine target"):
        _refiner(SectionLLM("x")).refine(_QUIZ, "nonsense", "y")


def test_no_llm_rejects_cleanly():
    result = _refiner(None).refine(_QUIZ, "questions", "y")
    assert not result.ok and "no LLM" in result.errors[0]
