"""The assessment blueprint + layout: spec validation, the type catalogue, and the
deterministic shuffling that keeps a printed sheet and its answer key in agreement."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from lessonforge.domain.assessment import (
    MAX_TOTAL,
    PRESETS,
    QUESTION_TYPES,
    AssessmentSpec,
    TypeSpec,
    build_sections,
    flat_views,
    full_marks,
    view,
)
from lessonforge.domain.ldd import Question, QuestionType


def _q(**kw):
    base = {"id": "Q1", "type": "short_answer", "prompt": "What?", "answer": "x",
            "objective_ids": ["O1"]}
    return Question.model_validate({**base, **kw})


def _match(qid="Q1"):
    return _q(id=qid, type="matching", prompt="Match", answer="",
              pairs=[{"left": "a", "right": "1"}, {"left": "b", "right": "2"},
                     {"left": "c", "right": "3"}, {"left": "d", "right": "4"}])


# ── catalogue ────────────────────────────────────────────────────────────────
def test_every_question_type_has_catalogue_row_and_valid_example():
    assert set(QUESTION_TYPES) == set(QuestionType)  # add a type → add its row
    for t, m in QUESTION_TYPES.items():
        q = Question.model_validate(m.example)       # examples must be valid questions
        assert q.type is t, t


def test_presets_are_valid_specs():
    for p in PRESETS:
        AssessmentSpec.model_validate({"types": p["types"]})


# ── spec validation ──────────────────────────────────────────────────────────
def test_spec_rejects_duplicate_types():
    with pytest.raises(ValidationError, match="once"):
        AssessmentSpec(types=[TypeSpec(type="mcq", count=2), TypeSpec(type="mcq", count=3)])


def test_spec_caps_total_questions():
    rows = [TypeSpec(type=t, count=25) for t in list(QuestionType)[:3]]
    assert sum(r.count for r in rows) > MAX_TOTAL
    with pytest.raises(ValidationError, match="too many"):
        AssessmentSpec(types=rows)


def test_spec_totals():
    spec = AssessmentSpec(types=[TypeSpec(type="mcq", count=4, marks_each=1),
                                 TypeSpec(type="long_answer", count=2, marks_each=5)])
    assert spec.total_questions == 6 and spec.total_marks == 14


def test_spec_requires_at_least_one_type():
    with pytest.raises(ValidationError):
        AssessmentSpec(types=[])


# ── question shape guardrails ────────────────────────────────────────────────
@pytest.mark.parametrize("kw,msg", [
    ({"type": "matching", "pairs": [{"left": "a", "right": "b"}]}, "at least 2 pairs"),
    ({"type": "ordering", "options": ["only one"]}, "at least 2 steps"),
    ({"type": "fill_blank", "prompt": "no blank here"}, "blank"),
    ({"type": "numerical", "answer": " "}, "require an answer"),
    ({"type": "mcq"}, "require options"),
])
def test_question_shape_is_enforced(kw, msg):
    with pytest.raises(ValidationError, match=msg):
        _q(**kw)


def test_matching_and_ordering_may_omit_the_answer():
    _match()
    _q(type="ordering", options=["a", "b", "c"], answer="")


# ── deterministic views ──────────────────────────────────────────────────────
def test_matching_key_is_consistent_with_the_printed_columns():
    q = _match()
    v = view(q, 1)
    # the key "1–C" means: left item 1's partner sits at letter C in the printed column B
    right_by_letter = {line.split(". ", 1)[0]: line.split(". ", 1)[1] for line in v.right}
    for token in v.answer.split(","):
        n, letter = token.strip().split("–")
        left = q.pairs[int(n) - 1]
        assert right_by_letter[letter] == left.right


def test_matching_columns_are_not_in_answer_order_and_are_stable():
    q = _match()
    v1, v2 = view(q, 1), view(q, 1)
    assert v1.right == v2.right and v1.answer == v2.answer           # deterministic
    assert [r.split(". ", 1)[1] for r in v1.right] != ["1", "2", "3", "4"]  # never the key order


def test_ordering_key_reconstructs_the_correct_sequence():
    steps = ["boil", "cool", "filter", "drink"]
    q = _q(type="ordering", prompt="Arrange", options=steps, answer="")
    v = view(q, 1)
    shown = {c.split(". ", 1)[0]: c.split(". ", 1)[1] for c in v.choices}
    assert [shown[ltr] for ltr in v.answer.split(" → ")] == steps
    assert [c.split(". ", 1)[1] for c in v.choices] != steps


def test_two_item_shuffle_never_equals_identity():
    q = _q(type="ordering", prompt="p", options=["a", "b"], answer="")
    assert [c.split(". ", 1)[1] for c in view(q, 1).choices] == ["b", "a"]


# ── sectioned layout ─────────────────────────────────────────────────────────
def test_no_spec_means_legacy_flat_layout():
    assert build_sections([_q()], None) is None
    assert [v.number for v in flat_views([_q(), _q(id="Q2")])] == [1, 2]


def test_sections_follow_blueprint_order_and_number_continuously():
    spec = AssessmentSpec(types=[TypeSpec(type="short_answer", count=1, marks_each=2),
                                 TypeSpec(type="mcq", count=2, marks_each=1)])
    mcq = lambda i: _q(id=f"M{i}", type="mcq", options=["a", "b"], answer="a")
    secs = build_sections([mcq(1), _q(), mcq(2)], spec)
    assert [s.type for s in secs] == [QuestionType.short_answer, QuestionType.mcq]
    assert [s.letter for s in secs] == ["A", "B"]
    assert [v.number for s in secs for v in s.items] == [1, 2, 3]
    assert secs[1].marks_note == "2 × 1 = 2 marks" and secs[0].marks_note == "1 × 2 = 2 marks"
    assert full_marks(secs) == 4


def test_unplanned_type_is_appended_not_dropped():
    spec = AssessmentSpec(types=[TypeSpec(type="short_answer", count=1)])
    extra = _q(id="Q2", type="true_false", answer="True")
    secs = build_sections([_q(), extra], spec)
    assert [s.type for s in secs] == [QuestionType.short_answer, QuestionType.true_false]


def test_short_answer_gets_more_room_in_sections_than_in_legacy():
    assert view(_q(), 1).response_lines == 1
    assert view(_q(), 1, roomy=True).response_lines > 1
