"""The refine engine — scoped reprompt, two-layer validation, bounded cascade.

These run entirely on fakes: a ``SectionLLM`` that returns a canned value for
whichever section the prompt asks about, so the whole loop (splice, validate,
cascade, diff, score) is exercised deterministically with no model.
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest

from lessonforge.domain.refine import RefineRequest
from lessonforge.providers.base import LLMResult
from lessonforge.rag.grounding import GroundingBundle
from lessonforge.rag.retriever import RetrievedChunk
from lessonforge.services.critique import NoopCritic, StructuralCritic
from lessonforge.services.refine import Refiner
from tests.conftest import FakeLLM

_TARGET_RE = re.compile(r'Improve ONLY the "([^"]+)" section')


class SectionLLM(FakeLLM):
    """Returns a canned value for whichever section the scoped prompt names,
    wrapped in the ``{"section": ...}`` envelope the refiner expects. A ``"*"``
    key answers a whole-document refine with a full LDD dict."""

    def __init__(self, sections: dict[str, Any]) -> None:
        super().__init__(response={})
        self._sections = sections

    def complete(self, prompt, *, system=None, json_schema=None, temperature=None) -> LLMResult:
        self.calls.append({"prompt": prompt, "system": system, "schema": json_schema})
        m = _TARGET_RE.search(prompt)
        if m is None:  # whole-document prompt
            return LLMResult(text=json.dumps(self._sections["*"]), raw={})
        target = m.group(1)
        return LLMResult(text=json.dumps({"section": self._sections[target]}), raw={})


def _refiner(sections: dict[str, Any], *, critic=None, max_cascade=2) -> Refiner:
    return Refiner(
        llm=SectionLLM(sections),
        critic=critic or StructuralCritic(),
        max_cascade=max_cascade,
    )


def test_scoped_refine_changes_only_the_target(valid_ldd):
    new_hook = {"prompt": "The river near your village floods every monsoon — why?",
                "kind": "scenario"}
    result = _refiner({"engagement_hook": new_hook}).refine(
        valid_ldd, RefineRequest(target="engagement_hook", instruction="use the river")
    )
    assert result.ok
    assert result.candidate.engagement_hook.kind == "scenario"
    # narrow output → the diff touches exactly one section
    assert set(result.diff) == {"engagement_hook"}
    assert result.applied is False  # propose-only
    assert result.cascaded == []


def test_leaf_refine_reports_score_delta(weak_ldd):
    # weak lesson has no local_context (scores 0 on local_relevance); add some
    result = _refiner({"local_context": ["paddy field", "river", "goat"]}).refine(
        weak_ldd, RefineRequest(target="local_context", instruction="ground it locally")
    )
    assert result.ok
    assert result.score_after.local_relevance > result.score_before.local_relevance
    assert "→" in result.notes


def test_unknown_target_raises(valid_ldd):
    with pytest.raises(ValueError, match="unknown refine target"):
        _refiner({}).refine(valid_ldd, RefineRequest(target="nonsense", instruction="x"))


def test_malformed_section_is_rejected_with_reason(valid_ldd):
    # a hook that opens with a definition trips the LDD's own field validator
    bad_hook = {"prompt": "Definition: the environment is everything around us",
                "kind": "question"}
    result = _refiner({"engagement_hook": bad_hook}).refine(
        valid_ldd, RefineRequest(target="engagement_hook", instruction="define it")
    )
    assert not result.ok
    assert result.candidate is None
    assert "invalid" in result.notes


def test_non_json_response_degrades_gracefully(valid_ldd):
    class BadLLM(FakeLLM):
        def complete(self, *a, **k):
            return LLMResult(text="sorry, I cannot help with that")

    refiner = Refiner(llm=BadLLM(), critic=StructuralCritic())
    result = refiner.refine(
        valid_ldd, RefineRequest(target="materials", instruction="add materials")
    )
    assert not result.ok
    assert "did not return valid JSON" in result.notes


def test_no_llm_is_unavailable_not_a_crash(valid_ldd):
    refiner = Refiner(llm=None, critic=StructuralCritic())
    result = refiner.refine(
        valid_ldd, RefineRequest(target="materials", instruction="add materials")
    )
    assert not result.ok
    assert "no LLM" in result.notes


def test_coupling_break_triggers_bounded_cascade(valid_ldd):
    # Add a second objective O2. On its own that breaks the guardrails (O2 is
    # neither taught nor assessed). The reviser must cascade into phases and
    # formative_checks — declared as O2's coupled neighbours — to keep it valid.
    new_objectives = [
        {"id": "O1", "statement": "Classify components into biotic and abiotic",
         "bloom": "understand"},
        {"id": "O2", "statement": "Explain why abiotic factors matter", "bloom": "apply"},
    ]
    new_phases = [
        {"name_en": "Engage", "teacher_activities": ["Ask what students saw"],
         "student_activities": ["Share one observation"], "minutes": 20,
         "objective_ids": ["O1", "O2"]},
    ]
    new_checks = [
        {"id": "Q1", "type": "short_answer", "prompt": "Name a biotic component",
         "answer": "goat", "objective_ids": ["O1"]},
        {"id": "Q2", "type": "short_answer", "prompt": "Why does sunlight matter?",
         "answer": "energy", "objective_ids": ["O2"]},
    ]
    refiner = _refiner({
        "objectives": new_objectives,
        "phases": new_phases,
        "formative_checks": new_checks,
    })
    result = refiner.refine(
        valid_ldd, RefineRequest(target="objectives", instruction="add an objective")
    )
    assert result.ok, result.errors
    assert {o.id for o in result.candidate.objectives} == {"O1", "O2"}
    assert result.cascaded == ["phases", "formative_checks"]
    assert set(result.diff) == {"objectives", "phases", "formative_checks"}


def test_unrepairable_coupling_is_rejected(valid_ldd):
    # Add O2 but the cascade "repairs" leave O2 still unassessed → reject-with-reason,
    # never force an invalid lesson through.
    new_objectives = [
        {"id": "O1", "statement": "Classify components into biotic and abiotic",
         "bloom": "understand"},
        {"id": "O2", "statement": "Explain why abiotic factors matter", "bloom": "apply"},
    ]
    # phases cover both, but checks still only cover O1
    new_phases = [
        {"name_en": "Engage", "teacher_activities": ["Ask"], "student_activities": ["Share"],
         "minutes": 20, "objective_ids": ["O1", "O2"]},
    ]
    stale_checks = [
        {"id": "Q1", "type": "short_answer", "prompt": "Name a biotic component",
         "answer": "goat", "objective_ids": ["O1"]},
    ]
    refiner = _refiner({
        "objectives": new_objectives,
        "phases": new_phases,
        "formative_checks": stale_checks,
    })
    result = refiner.refine(
        valid_ldd, RefineRequest(target="objectives", instruction="add an objective")
    )
    assert not result.ok
    assert result.candidate is None
    assert "breaks the lesson's structure" in result.notes


def test_cascade_disabled_rejects_coupling_break(valid_ldd):
    new_objectives = [
        {"id": "O1", "statement": "Classify components into biotic and abiotic",
         "bloom": "understand"},
        {"id": "O2", "statement": "Explain why abiotic factors matter", "bloom": "apply"},
    ]
    refiner = _refiner({"objectives": new_objectives}, max_cascade=0)
    result = refiner.refine(
        valid_ldd, RefineRequest(target="objectives", instruction="add an objective")
    )
    assert not result.ok
    assert result.cascaded == []


def test_grounding_sources_are_authoritative_across_refine(valid_ldd):
    valid_ldd.quality.grounding_sources = ["CDC Science Grade 6, Unit 2"]
    result = _refiner({"materials": ["Component cards", "Chart paper", "Markers"]}).refine(
        valid_ldd, RefineRequest(target="materials", instruction="add materials")
    )
    assert result.ok
    # a reprompt can improve wording but cannot invent or drop citations
    assert result.candidate.quality.grounding_sources == ["CDC Science Grade 6, Unit 2"]


def test_whole_document_refine(valid_ldd, export_ldd_dict):
    result = _refiner({"*": export_ldd_dict}).refine(
        valid_ldd, RefineRequest(target="*", instruction="make it all about the river")
    )
    assert result.ok
    assert result.candidate.topic == export_ldd_dict["topic"]
    assert len(result.candidate.objectives) == 2


class _StubGrounding:
    """Duck-typed GroundingRetriever: returns a canned bundle and records the
    ground() calls so a test can assert retrieval happened exactly once."""

    def __init__(self, bundle: GroundingBundle) -> None:
        self.bundle = bundle
        self.calls: list[dict[str, Any]] = []

    def ground(self, *, query, grade=None, subject=None, framework=None) -> GroundingBundle:
        self.calls.append(
            {"query": query, "grade": grade, "subject": subject, "framework": framework}
        )
        return self.bundle


def _grounded_bundle() -> GroundingBundle:
    return GroundingBundle(
        chunks={
            "reference": [
                RetrievedChunk("Photosynthesis converts light into chemical energy.",
                               1.0, {"source": "My Science Grade 6, Unit 4"})
            ]
        },
        sources=["My Science Grade 6, Unit 4"],
        authoritative=frozenset({"reference"}),
    )


def test_grounded_refine_injects_reference_material_into_prompt(valid_ldd):
    # a grounded target (materials) must retrieve once and show the model the
    # reference text — the whole point of the refine-grounding fix.
    grounding = _StubGrounding(_grounded_bundle())
    refiner = Refiner(
        llm=SectionLLM({"materials": ["Leaf samples", "Beaker", "Iodine"]}),
        critic=StructuralCritic(),
        grounding=grounding,
    )
    result = refiner.refine(
        valid_ldd, RefineRequest(target="materials", instruction="use lab equipment")
    )
    assert result.ok
    # retrieved exactly once (retrieve-once-use-twice), on the original doc's keys
    assert len(grounding.calls) == 1
    assert grounding.calls[0]["grade"] == valid_ldd.curriculum_ref.grade
    # the reference material reached the model's prompt
    section_prompt = refiner.llm.calls[0]["prompt"]
    assert "Photosynthesis converts light" in section_prompt
    assert "Ground your revision in this reference material" in section_prompt
    # and its source was merged into the candidate's provenance
    assert "My Science Grade 6, Unit 4" in result.candidate.quality.grounding_sources


def test_grounded_refine_merges_not_replaces_sources(valid_ldd):
    valid_ldd.quality.grounding_sources = ["CDC Science Grade 6, Unit 2"]
    grounding = _StubGrounding(_grounded_bundle())
    refiner = Refiner(
        llm=SectionLLM({"materials": ["Leaf samples", "Beaker", "Iodine"]}),
        critic=StructuralCritic(),
        grounding=grounding,
    )
    result = refiner.refine(
        valid_ldd, RefineRequest(target="materials", instruction="use lab equipment")
    )
    assert result.ok
    # union: the original citation is preserved AND the new one added
    assert result.candidate.quality.grounding_sources == [
        "CDC Science Grade 6, Unit 2",
        "My Science Grade 6, Unit 4",
    ]


def test_grounding_retrieval_is_skipped_when_bundle_is_empty(valid_ldd):
    # an empty bundle must not add a grounding block to the prompt (no noise)
    grounding = _StubGrounding(GroundingBundle(authoritative=frozenset({"reference"})))
    refiner = Refiner(
        llm=SectionLLM({"materials": ["Cards", "Chart", "Markers"]}),
        critic=StructuralCritic(),
        grounding=grounding,
    )
    result = refiner.refine(
        valid_ldd, RefineRequest(target="materials", instruction="add materials")
    )
    assert result.ok
    assert "Ground your revision in this reference material" not in refiner.llm.calls[0]["prompt"]


def test_noop_critic_scores_are_flat(valid_ldd):
    result = _refiner(
        {"materials": ["Cards", "Chart", "Markers"]}, critic=NoopCritic()
    ).refine(valid_ldd, RefineRequest(target="materials", instruction="add materials"))
    assert result.ok
    assert result.score_before.specificity == 1.0
    assert result.score_after.specificity == 1.0
