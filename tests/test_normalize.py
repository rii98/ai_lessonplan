"""Layer 1 — the normalization layer. Meaning-preserving, drift-proof, unbreakable.

These are deliberately heavy on edge cases: the whole point of this layer is to
absorb the messy variety of what a real model emits, so the strict schema (and
the expensive repair loop) never has to.
"""

from __future__ import annotations

import copy

import pytest

from lessonforge.domain.ldd import (
    BloomLevel,
    Hook,
    LessonDesignDocument,
    QuestionType,
)
from lessonforge.services.normalize import (
    BLOOMS,
    KINDS,
    Q_TYPES,
    _snap,
    normalize_ldd,
)


# ── the derived legal surface stays in sync with the models (anti-drift) ──────
def test_allowed_sets_match_the_models():
    assert set(KINDS) == set(Hook.model_fields["kind"].annotation.__args__)
    assert set(BLOOMS) == {b.value for b in BloomLevel}
    assert set(Q_TYPES) == {q.value for q in QuestionType}


# ── _snap: the enum-snapping core ─────────────────────────────────────────────
@pytest.mark.parametrize("value,expected", [
    ("analogy", "scenario"),      # the field regression (synonym)
    ("Analogy!", "scenario"),     # punctuation + case
    ("DEMO", "demonstration"),    # synonym, upper
    ("demonstration", "demonstration"),  # already valid
    ("prediction", "prediction"),
    ("quiz-question", "question"),  # substring match
    ("role play", "scenario"),    # spaced synonym
    ("vibes", "question"),        # no match at all → default
])
def test_snap_hook_kind(value, expected):
    got, _ = _snap(value, KINDS, "question", "kind")
    assert got == expected


@pytest.mark.parametrize("value,expected", [
    ("understanding", "understand"), ("Applying", "apply"), ("analysis", "analyze"),
    ("synthesis", "create"), ("recall", "remember"), ("understand", "understand"),
])
def test_snap_bloom(value, expected):
    got, _ = _snap(value, BLOOMS, "understand", "bloom")
    assert got == expected


@pytest.mark.parametrize("value,expected", [
    ("multiple choice", "mcq"), ("MCQ", "mcq"), ("true/false", "true_false"),
    ("T/F", "true_false"), ("essay", "short_answer"), ("fill in the blank", "short_answer"),
    ("short_answer", "short_answer"),
])
def test_snap_question_type(value, expected):
    got, _ = _snap(value, Q_TYPES, "short_answer", "type")
    assert got == expected


def test_snap_reports_changed_flag():
    assert _snap("scenario", KINDS, "question", "kind") == ("scenario", False)
    assert _snap("analogy", KINDS, "question", "kind")[1] is True
    # non-string junk snaps to the default and counts as changed
    assert _snap({"weird": 1}, KINDS, "question", "kind") == ("question", True)
    assert _snap(None, KINDS, "question", "kind") == ("question", True)


# ── whole-document normalization ──────────────────────────────────────────────
def test_valid_ldd_is_untouched(valid_ldd_dict):
    out, notes = normalize_ldd(valid_ldd_dict)
    assert notes == []
    assert out == valid_ldd_dict  # no spurious changes to an already-clean doc


def test_normalize_does_not_mutate_input(valid_ldd_dict):
    bad = copy.deepcopy(valid_ldd_dict)
    bad["engagement_hook"]["kind"] = "analogy"
    snapshot = copy.deepcopy(bad)
    normalize_ldd(bad)
    assert bad == snapshot  # input dict is never mutated (works on a deep copy)


def test_normalize_snaps_enums_and_records_notes(valid_ldd_dict):
    bad = copy.deepcopy(valid_ldd_dict)
    bad["engagement_hook"]["kind"] = "analogy"
    bad["objectives"][0]["bloom"] = "understanding"
    bad["formative_checks"][0]["type"] = "multiple choice"
    bad["language"] = "English"
    bad["framework"] = "5e"
    out, notes = normalize_ldd(bad)
    assert out["engagement_hook"]["kind"] == "scenario"
    assert out["objectives"][0]["bloom"] == "understand"
    assert out["formative_checks"][0]["type"] == "mcq"
    assert out["language"] == "en"
    assert out["framework"] == "5E"
    assert len(notes) == 5  # every change is reported


def test_normalize_coerces_numbers_and_duration(valid_ldd_dict):
    bad = copy.deepcopy(valid_ldd_dict)
    bad["duration_min"] = "50 minutes"   # non-int, off the 30/45/60 grid
    bad["phases"][0]["minutes"] = "5"    # stringified int
    bad["curriculum_ref"]["grade"] = "6"
    out, _ = normalize_ldd(bad)
    assert out["duration_min"] == 45     # snapped to nearest legal duration
    assert out["phases"][0]["minutes"] == 5
    assert out["curriculum_ref"]["grade"] == 6


def test_normalize_wraps_scalar_prose_into_lists(valid_ldd_dict):
    bad = copy.deepcopy(valid_ldd_dict)
    bad["local_context"] = "paddy field"          # scalar → single-element list
    bad["materials"] = "chart paper, markers"     # prose: NOT split on the comma
    out, _ = normalize_ldd(bad)
    assert out["local_context"] == ["paddy field"]
    assert out["materials"] == ["chart paper, markers"]


def test_normalize_splits_csv_ids_but_not_prose(valid_ldd_dict):
    bad = copy.deepcopy(valid_ldd_dict)
    bad["phases"][0]["objective_ids"] = "O1, O1"  # id csv → split
    out, _ = normalize_ldd(bad)
    assert out["phases"][0]["objective_ids"] == ["O1", "O1"]


def test_normalize_clears_options_on_non_mcq(valid_ldd_dict):
    bad = copy.deepcopy(valid_ldd_dict)
    bad["formative_checks"][0]["type"] = "short_answer"
    bad["formative_checks"][0]["options"] = ["a", "b"]  # illegal on non-MCQ
    out, _ = normalize_ldd(bad)
    assert out["formative_checks"][0]["options"] is None


def test_normalized_analogy_doc_now_validates(valid_ldd_dict):
    # the end-to-end promise: a doc that FAILED validation passes after Layer 1
    bad = copy.deepcopy(valid_ldd_dict)
    bad["engagement_hook"]["kind"] = "analogy"
    out, _ = normalize_ldd(bad)
    LessonDesignDocument.model_validate(out)  # no raise


# ── robustness: normalize must never throw, whatever the shape ────────────────
@pytest.mark.parametrize("junk", [
    None, [], "a string", 42,
    {"engagement_hook": "not a dict"},
    {"objectives": "nope", "phases": None, "formative_checks": [123, {"type": "mc"}]},
    {"curriculum_ref": [1, 2], "duration_min": {"x": 1}},
])
def test_normalize_never_raises_on_garbage(junk):
    _, notes = normalize_ldd(junk)  # must not raise
    assert isinstance(notes, list)
