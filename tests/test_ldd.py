"""The LDD validators ARE the anti-generic guardrails. Test that they bite."""

from __future__ import annotations

import copy

import pytest
from pydantic import ValidationError

from lessonforge.domain.ldd import LessonDesignDocument


def test_valid_ldd_constructs(valid_ldd_dict):
    ldd = LessonDesignDocument.model_validate(valid_ldd_dict)
    assert ldd.topic.startswith("Components")
    assert ldd.objectives[0].id == "O1"


def test_hook_may_not_be_a_definition(valid_ldd_dict):
    d = copy.deepcopy(valid_ldd_dict)
    d["engagement_hook"]["prompt"] = "Definition: the environment is everything around us"
    with pytest.raises(ValidationError, match="definition"):
        LessonDesignDocument.model_validate(d)


def test_objective_must_be_assessed(valid_ldd_dict):
    d = copy.deepcopy(valid_ldd_dict)
    d["formative_checks"] = []  # objective O1 now untested
    with pytest.raises(ValidationError, match="not covered by any formative check"):
        LessonDesignDocument.model_validate(d)


def test_objective_must_be_taught(valid_ldd_dict):
    d = copy.deepcopy(valid_ldd_dict)
    d["phases"][0]["objective_ids"] = []  # O1 not taught by any phase
    with pytest.raises(ValidationError, match="not addressed by any phase"):
        LessonDesignDocument.model_validate(d)


def test_phase_cannot_reference_unknown_objective(valid_ldd_dict):
    d = copy.deepcopy(valid_ldd_dict)
    d["phases"][0]["objective_ids"] = ["O99"]
    with pytest.raises(ValidationError, match="unknown objectives"):
        LessonDesignDocument.model_validate(d)


def test_duplicate_objective_ids_rejected(valid_ldd_dict):
    d = copy.deepcopy(valid_ldd_dict)
    d["objectives"].append(dict(d["objectives"][0]))  # duplicate O1
    with pytest.raises(ValidationError, match="unique"):
        LessonDesignDocument.model_validate(d)


def test_mcq_requires_options(valid_ldd_dict):
    d = copy.deepcopy(valid_ldd_dict)
    d["formative_checks"][0] = {
        "id": "Q1", "type": "mcq", "prompt": "Which is biotic?",
        "answer": "goat", "objective_ids": ["O1"],  # no options
    }
    with pytest.raises(ValidationError, match="options"):
        LessonDesignDocument.model_validate(d)
