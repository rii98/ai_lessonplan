"""Blueprint-driven quiz/worksheet generation: the prompt carries the exact
blueprint, and the counts the prompt can only *ask* for are then *enforced*."""

from __future__ import annotations

import json

import pytest

from lessonforge.domain.artifacts import Quiz, Worksheet
from lessonforge.domain.assessment import AssessmentSpec, TypeSpec
from lessonforge.domain.ldd import NormalizedBrief
from lessonforge.export import ArtifactKind
from lessonforge.providers.base import LLMClient, LLMResult
from lessonforge.rag.documents import Collection, Document
from lessonforge.rag.grounding import GroundingRetriever
from lessonforge.rag.ingest import Ingestor
from lessonforge.rag.retriever import Retriever
from lessonforge.services.artifact_generation import build_generator

_BRIEF = NormalizedBrief(topic="Sound and Vibration", grade=7, subject="Science")
_REF = {"board": "CDC", "grade": 7, "subject": "Science", "code": None}
_OBJ = [{"id": "O1", "statement": "Explain that sound is a vibration", "bloom": "understand"}]


class SeqLLM(LLMClient):
    """Returns queued responses in order (the last one repeats) and records prompts."""

    def __init__(self, *responses):
        self.responses, self.prompts = list(responses), []

    def complete(self, prompt, *, system=None, json_schema=None, temperature=None):
        self.prompts.append(prompt)
        r = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        return LLMResult(text=r if isinstance(r, str) else json.dumps(r), raw={})

    def health(self):
        return True


def _mcq(i, **kw):
    return {"id": f"X{i}", "type": "mcq", "prompt": f"MCQ {i}?", "options": ["a", "b", "c", "d"],
            "answer": "a", "objective_ids": ["O1"], **kw}


def _tf(i, **kw):
    return {"id": f"T{i}", "type": "true_false", "prompt": f"Statement {i}", "answer": "True",
            "objective_ids": ["O1"], **kw}


def _quiz(questions, **kw):
    return {"topic": "Sound", "curriculum_ref": _REF, "objectives": _OBJ,
            "questions": questions, **kw}


def _spec(**rows):
    return AssessmentSpec(types=[TypeSpec(type=t, **v) for t, v in rows.items()])


def _gen(kind=ArtifactKind.quiz, llm=None, **kw):
    return build_generator(kind, llm=llm, **kw)


# ── the prompt ───────────────────────────────────────────────────────────────
def test_prompt_carries_blueprint_rules_and_instruction_for_only_the_chosen_types():
    llm = SeqLLM(_quiz([_mcq(1), _tf(1)]))
    spec = _spec(mcq={"count": 1, "difficulty": "hard", "marks_each": 2,
                      "instruction": "use the madal as an example"},
                 true_false={"count": 1})
    _gen(llm=llm).generate(_BRIEF, spec=spec)
    p = llm.prompts[0]
    assert "1 × mcq" in p and "difficulty: hard" in p and "2 mark(s) each" in p
    assert "use the madal as an example" in p
    assert "- mcq:" in p and "- true_false:" in p
    assert "matching" not in p and "numerical" not in p  # nothing about unchosen types
    assert "required counts" in " ".join(p.split())


def test_without_a_spec_the_legacy_prompt_is_used():
    llm = SeqLLM(_quiz([_mcq(1)]))
    quiz = _gen(llm=llm).generate(_BRIEF)
    assert "BLUEPRINT" not in llm.prompts[0]
    assert quiz.spec is None


def test_slides_reject_a_blueprint():
    with pytest.raises(ValueError, match="no question blueprint"):
        _gen(ArtifactKind.slides, llm=SeqLLM({})).generate(_BRIEF, spec=_spec(mcq={"count": 1}))


# ── enforcement ──────────────────────────────────────────────────────────────
def test_extras_are_trimmed_unrequested_types_dropped_and_ids_renumbered():
    raw = _quiz([_mcq(1), _mcq(2), _mcq(3), _tf(1),
                 {"id": "S", "type": "short_answer", "prompt": "?", "answer": "x",
                  "objective_ids": ["O1"]}])
    quiz = _gen(llm=SeqLLM(raw)).generate(_BRIEF, spec=_spec(mcq={"count": 2}, true_false={"count": 1}))
    assert [q.type.value for q in quiz.questions] == ["mcq", "mcq", "true_false"]
    assert [q.id for q in quiz.questions] == ["Q1", "Q2", "Q3"]
    assert quiz.notes == []


def test_questions_are_ordered_by_the_blueprint_not_by_the_model():
    quiz = _gen(llm=SeqLLM(_quiz([_tf(1), _mcq(1)]))).generate(
        _BRIEF, spec=_spec(mcq={"count": 1}, true_false={"count": 1}))
    assert [q.type.value for q in quiz.questions] == ["mcq", "true_false"]


def test_requested_difficulty_and_marks_are_authoritative():
    raw = _quiz([_mcq(1, difficulty="easy"), _tf(1, marks=7)])
    quiz = _gen(llm=SeqLLM(raw)).generate(
        _BRIEF, spec=_spec(mcq={"count": 1, "difficulty": "hard", "marks_each": 2},
                           true_false={"count": 1, "difficulty": "mixed", "marks_each": 1}))
    mcq, tf = quiz.questions
    assert mcq.difficulty.value == "hard" and mcq.marks == 2     # forced to the request
    assert tf.marks == 7 and tf.difficulty is None               # mixed → keep the model's; own marks kept


def test_mixed_keeps_the_models_per_question_difficulty_snapped_to_legal_values():
    raw = _quiz([_mcq(1, difficulty="Challenging"), _mcq(2, difficulty="simple")])
    quiz = _gen(llm=SeqLLM(raw)).generate(_BRIEF, spec=_spec(mcq={"count": 2}))
    assert [q.difficulty.value for q in quiz.questions] == ["hard", "easy"]


def test_shortfall_is_topped_up_once_with_only_the_missing_questions():
    first = _quiz([_mcq(1)])
    top_up = {"questions": [_mcq(2), _mcq(3)]}
    llm = SeqLLM(first, top_up)
    quiz = _gen(llm=llm).generate(_BRIEF, spec=_spec(mcq={"count": 3}))
    assert len(quiz.questions) == 3 and quiz.notes == []
    assert len(llm.prompts) == 2
    assert "2 × mcq" in llm.prompts[1] and "MCQ 1?" in llm.prompts[1]  # asks for 2; shows existing


def test_unfixable_shortfall_is_reported_honestly_not_hidden():
    llm = SeqLLM(_quiz([_mcq(1)]), "not json at all")
    quiz = _gen(llm=llm).generate(_BRIEF, spec=_spec(mcq={"count": 3}))
    assert len(quiz.questions) == 1
    assert quiz.notes and "1 of 3" in quiz.notes[0]


def test_top_up_questions_with_unknown_objectives_are_ignored():
    bad = _mcq(2, objective_ids=["O9"])
    quiz = _gen(llm=SeqLLM(_quiz([_mcq(1)]), {"questions": [bad]})).generate(
        _BRIEF, spec=_spec(mcq={"count": 2}))
    assert len(quiz.questions) == 1 and quiz.notes


def test_a_quiz_with_none_of_the_requested_types_fails_cleanly():
    with pytest.raises(ValueError, match="no questions of the requested types"):
        _gen(llm=SeqLLM(_quiz([_tf(1)]), {"questions": []})).generate(
            _BRIEF, spec=_spec(mcq={"count": 1}))


def test_spec_is_stamped_and_a_model_echoed_spec_cannot_break_validation():
    junk = _quiz([_mcq(1)], spec={"types": "garbage"}, notes="x")
    spec = _spec(mcq={"count": 1})
    quiz = _gen(llm=SeqLLM(junk)).generate(_BRIEF, spec=spec)
    assert isinstance(quiz, Quiz) and quiz.spec == spec


def test_every_new_type_round_trips_through_generation():
    qs = [
        {"id": "a", "type": "fill_in_the_blank", "prompt": "Sound is a _____.", "answer": "vibration",
         "objective_ids": ["O1"]},
        {"id": "b", "type": "Match the following", "prompt": "Match", "objective_ids": ["O1"],
         "pairs": [["madal", "skin"], ["flute", "air"]]},
        {"id": "c", "type": "explain", "prompt": "Why?", "answer": "because", "objective_ids": ["O1"],
         "key_points": "one point"},
        {"id": "d", "type": "calculation", "prompt": "2+2?", "answer": "4", "objective_ids": ["O1"]},
        {"id": "e", "type": "arrange", "prompt": "Order", "options": ["x", "y", "z"],
         "objective_ids": ["O1"]},
    ]
    spec = _spec(fill_blank={"count": 1}, matching={"count": 1}, long_answer={"count": 1},
                 numerical={"count": 1}, ordering={"count": 1})
    quiz = _gen(llm=SeqLLM(_quiz(qs))).generate(_BRIEF, spec=spec)
    assert [q.type.value for q in quiz.questions] == [
        "fill_blank", "matching", "long_answer", "numerical", "ordering"]
    assert quiz.questions[1].pairs[0].left == "madal"      # [l, r] list → pair
    assert quiz.questions[2].key_points == ["one point"]   # scalar → list


def test_worksheet_honours_a_blueprint_too():
    ws = {"topic": "Sound", "curriculum_ref": _REF, "objectives": _OBJ,
          "tasks": ["Hum and feel your throat."], "questions": [_mcq(1), _mcq(2), _mcq(3)]}
    out = _gen(ArtifactKind.worksheet, llm=SeqLLM(ws)).generate(_BRIEF, spec=_spec(mcq={"count": 2}))
    assert isinstance(out, Worksheet) and len(out.questions) == 2 and out.tasks


# ── retrieval wiring (real GroundingRetriever + fakes) ───────────────────────
def test_blueprint_generation_runs_type_aware_retrieval(fake_embedder, fake_store, fake_reranker):
    Ingestor(embedder=fake_embedder, vector_store=fake_store).ingest_documents(
        Collection.reference,
        [Document(id="r1", text="Sound is produced by vibration of objects.", source="My Book",
                  metadata={"grade": 7, "subject": "Science"})])
    grounding = GroundingRetriever(
        Retriever(embedder=fake_embedder, vector_store=fake_store, reranker=fake_reranker))
    seen = []
    real = grounding.ground
    grounding.ground = lambda **kw: (seen.append((kw["granularity"], kw["top_n"])), real(**kw))[1]
    quiz = _gen(llm=SeqLLM(_quiz([_mcq(1), {"id": "L", "type": "long_answer", "prompt": "Why?",
                                            "answer": "a", "objective_ids": ["O1"]}])),
                grounding=grounding).generate(
        _BRIEF, spec=_spec(mcq={"count": 1}, long_answer={"count": 1}))
    assert seen == [("narrow", 3), ("section", 2)]
    assert "My Book" in quiz.grounding_sources


def test_ground_top_n_overrides_the_configured_default(fake_embedder, fake_store, fake_reranker):
    docs = [Document(id=f"r{i}", text=f"Sound fact number {i} about vibration.", source=f"s{i}",
                     metadata={"grade": 7, "subject": "Science"}) for i in range(6)]
    Ingestor(embedder=fake_embedder, vector_store=fake_store).ingest_documents(Collection.reference, docs)
    g = GroundingRetriever(Retriever(embedder=fake_embedder, vector_store=fake_store,
                                     reranker=fake_reranker))
    few = g.ground(query="sound vibration", grade=7, subject="Science", top_n=1)
    many = g.ground(query="sound vibration", grade=7, subject="Science", top_n=5)
    assert len(few.chunks["reference"]) == 1 < len(many.chunks["reference"])
