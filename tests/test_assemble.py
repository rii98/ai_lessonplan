"""Layers 2 & 3 — the assembler: normalize → validate → repair → degrade.

Covers the four exits: valid first try, fixed by normalization alone (no model
call), fixed by a bounded repair round, and graceful failure (no crash) when it
can't be fixed or there's no model to help.
"""

from __future__ import annotations

import copy
import json

from lessonforge.providers.base import LLMClient, LLMResult
from lessonforge.services.assemble import LDDAssembler
from tests.conftest import FakeLLM


class ScriptLLM(LLMClient):
    """Returns a queued sequence of dict responses, one per call."""

    def __init__(self, *responses, structured=False):
        self._responses = list(responses)
        self._structured = structured
        self.calls = 0

    def complete(self, prompt, *, system=None, json_schema=None, temperature=None) -> LLMResult:
        self.calls += 1
        doc = self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]
        return LLMResult(text=json.dumps(doc))

    def health(self) -> bool:
        return True

    @property
    def supports_structured_output(self) -> bool:
        return self._structured


def _unassessed(valid_ldd_dict):
    bad = copy.deepcopy(valid_ldd_dict)
    bad["formative_checks"] = []  # O1 taught but not assessed → coupling break
    return bad


def test_valid_input_passes_first_try(valid_ldd_dict):
    out = LDDAssembler(llm=None).assemble(valid_ldd_dict)
    assert out.ok and out.repairs == 0 and out.notes == []
    assert out.ldd.objectives[0].id == "O1"


def test_accepts_a_json_string(valid_ldd_dict):
    out = LDDAssembler(llm=None).assemble(json.dumps(valid_ldd_dict))
    assert out.ok


def test_normalization_alone_fixes_without_calling_model(valid_ldd_dict):
    bad = copy.deepcopy(valid_ldd_dict)
    bad["engagement_hook"]["kind"] = "analogy"
    llm = FakeLLM()  # would count calls if used
    out = LDDAssembler(llm=llm, max_repairs=2).assemble(bad)
    assert out.ok and out.repairs == 0
    assert out.ldd.engagement_hook.kind == "scenario"
    assert out.notes  # recorded the snap
    assert llm.calls == []  # Layer 1 handled it → no repair round spent


def test_semantic_failure_recovered_by_one_repair(valid_ldd_dict):
    llm = ScriptLLM(valid_ldd_dict)  # the "repair" returns a valid doc
    out = LDDAssembler(llm=llm, max_repairs=1).assemble(_unassessed(valid_ldd_dict))
    assert out.ok and out.repairs == 1
    assert llm.calls == 1


def test_unfixable_failure_degrades_not_crashes(valid_ldd_dict):
    bad = _unassessed(valid_ldd_dict)
    llm = ScriptLLM(bad)  # repair keeps returning the same broken doc
    out = LDDAssembler(llm=llm, max_repairs=2).assemble(bad)
    assert not out.ok
    assert out.ldd is None
    assert any("formative" in e or "assessed" in e.lower() for e in out.errors)


def test_no_llm_means_no_repair_just_a_clean_failure(valid_ldd_dict):
    out = LDDAssembler(llm=None).assemble(_unassessed(valid_ldd_dict))
    assert not out.ok and out.errors  # normalization couldn't help, nothing to repair with


def test_non_json_degrades_gracefully():
    out = LDDAssembler(llm=None).assemble("the model apologises and refuses")
    assert not out.ok
    assert "did not return JSON" in out.errors[0]


def test_repair_loop_skipped_when_backend_guarantees_structure(valid_ldd_dict):
    # a guided-decoding backend can't emit invalid output, so no repair budget is
    # spent even on a broken doc — it fails fast on the single validate.
    bad = _unassessed(valid_ldd_dict)
    llm = ScriptLLM(valid_ldd_dict, structured=True)
    out = LDDAssembler(llm=llm, max_repairs=3).assemble(bad)
    assert not out.ok
    assert llm.calls == 0  # repair loop skipped despite max_repairs=3
