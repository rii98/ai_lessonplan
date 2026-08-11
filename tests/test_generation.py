"""Generation service turns a brief into a validated LDD using the LLM port."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from lessonforge.domain.ldd import LessonDesignDocument, NormalizedBrief
from lessonforge.services.generation import LessonGenerator
from tests.conftest import FakeLLM


def test_generate_returns_validated_ldd(valid_ldd_dict):
    llm = FakeLLM(response=valid_ldd_dict)
    gen = LessonGenerator(llm=llm, grounding=None)
    brief = NormalizedBrief(topic="Environment", grade=6, subject="Science")

    ldd = gen.generate(brief)

    assert isinstance(ldd, LessonDesignDocument)
    assert ldd.objectives[0].id == "O1"
    # the LLM was asked for JSON constrained to the LDD schema
    assert llm.calls[0]["schema"]["title"] == "LessonDesignDocument"


def test_generate_rejects_structurally_bad_lesson(valid_ldd_dict):
    bad = dict(valid_ldd_dict)
    bad["formative_checks"] = []  # objective no longer assessed → must fail
    gen = LessonGenerator(llm=FakeLLM(response=bad), grounding=None)
    with pytest.raises(ValidationError):
        gen.generate(NormalizedBrief(topic="x", grade=6, subject="Science"))


def test_generate_raises_on_non_json():
    class BadLLM(FakeLLM):
        def complete(self, *a, **k):
            from lessonforge.providers.base import LLMResult
            return LLMResult(text="not json at all")

    gen = LessonGenerator(llm=BadLLM(), grounding=None)
    with pytest.raises(ValueError, match="valid JSON"):
        gen.generate(NormalizedBrief(topic="x", grade=6, subject="Science"))
