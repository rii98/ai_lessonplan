"""Generation service turns a brief into a validated LDD using the LLM port."""

from __future__ import annotations

import json

import pytest

from lessonforge.domain.ldd import LessonDesignDocument, NormalizedBrief
from lessonforge.providers.base import LLMResult
from lessonforge.services.generation import LessonGenerator
from tests.conftest import FakeLLM


def _brief() -> NormalizedBrief:
    return NormalizedBrief(topic="Environment", grade=6, subject="Science")


def test_generate_returns_validated_ldd(valid_ldd_dict):
    llm = FakeLLM(response=valid_ldd_dict)
    gen = LessonGenerator(llm=llm, grounding=None)

    ldd = gen.generate(_brief())

    assert isinstance(ldd, LessonDesignDocument)
    assert ldd.objectives[0].id == "O1"
    # the LLM was asked for JSON constrained to the LDD schema
    assert llm.calls[0]["schema"]["title"] == "LessonDesignDocument"


def test_generate_rejects_structurally_bad_lesson(valid_ldd_dict):
    bad = dict(valid_ldd_dict)
    bad["formative_checks"] = []  # objective no longer assessed → must fail
    # FakeLLM returns the same bad doc on repair too, so it can't be fixed →
    # generation degrades to a clean ValueError (no raw ValidationError, no 500).
    gen = LessonGenerator(llm=FakeLLM(response=bad), grounding=None, max_repairs=1)
    with pytest.raises(ValueError, match="could not generate a valid lesson"):
        gen.generate(_brief())


def test_generate_raises_on_non_json():
    class BadLLM(FakeLLM):
        def complete(self, *a, **k):
            return LLMResult(text="not json at all")

    gen = LessonGenerator(llm=BadLLM(), grounding=None)
    with pytest.raises(ValueError, match="did not return JSON"):
        gen.generate(_brief())


# ── Layer 1 in the generation path: the exact bug from the field ──────────────
def test_generate_normalizes_bad_enum_from_model(valid_ldd_dict):
    # the real regression: the model returned kind:"analogy" for "Java vs JavaScript"
    bad = json.loads(json.dumps(valid_ldd_dict))
    bad["engagement_hook"]["kind"] = "analogy"
    gen = LessonGenerator(llm=FakeLLM(response=bad), grounding=None)

    ldd = gen.generate(_brief())  # no longer raises

    assert ldd.engagement_hook.kind == "scenario"  # snapped, meaning preserved
    # transparent about the adjustment, on a field the critique stamp won't clobber
    assert any("analogy" in a for a in ldd.quality.adjustments)


def test_generate_repairs_semantic_failure_via_model(valid_ldd_dict):
    # Layer 2: first response is unfixable-by-normalization (an objective the model
    # forgot to assess); the repair round returns a valid doc.
    bad = json.loads(json.dumps(valid_ldd_dict))
    bad["formative_checks"] = []  # O1 taught but not assessed → coupling break

    class RepairLLM(FakeLLM):
        def __init__(self, bad_doc, good_doc):
            super().__init__()
            self._responses = [bad_doc, good_doc]

        def complete(self, *a, **k):
            self.calls.append({"schema": k.get("json_schema")})
            doc = self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]
            return LLMResult(text=json.dumps(doc))

    gen = LessonGenerator(llm=RepairLLM(bad, valid_ldd_dict), grounding=None, max_repairs=1)
    ldd = gen.generate(_brief())

    assert ldd.objectives[0].id == "O1"  # recovered after one repair round
