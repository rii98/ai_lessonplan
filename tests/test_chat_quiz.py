"""The quiz skill and the quiz engine: slots → blueprint, generation, the answer-free
play view, server-side grading for every type, attempts, review, retry-missed."""

from __future__ import annotations

import json

import pytest

from lessonforge.config import ChatConfig
from lessonforge.domain.chat import Conversation
from lessonforge.domain.ldd import Question, QuestionType
from lessonforge.providers.base import LLMClient, LLMResult
from lessonforge.services.chat.artifacts import ArtifactService
from lessonforge.services.chat.quiz_play import LLMGrader, QuizSessions, RuleGrader, play_view
from lessonforge.services.chat.skills import RoutedIntent, SkillContext, SkillDeps
from lessonforge.services.chat.skills.quiz import QuizSkill, build_spec, coerce_slots
from lessonforge.services.chat.store import MemoryChatStore

QT = QuestionType


class Script(LLMClient):
    def __init__(self, reply):
        self.reply, self.prompts = reply, []

    def complete(self, prompt, *, system=None, json_schema=None, temperature=None):
        self.prompts.append(prompt)
        if isinstance(self.reply, Exception):
            raise self.reply
        r = self.reply(prompt) if callable(self.reply) else self.reply
        return LLMResult(text=r if isinstance(r, str) else json.dumps(r), raw={})

    def health(self):
        return True


def _q(**kw):
    base = {"id": "Q1", "type": "mcq", "prompt": "p", "answer": "a", "objective_ids": ["O1"],
            "explanation": "because"}
    return Question.model_validate({**base, **kw})


# ── slots + blueprint ────────────────────────────────────────────────────────
def test_slots_are_clamped_and_cleaned():
    s = coerce_slots({"topic": "  Photosynthesis?! ", "count": "99", "types": "mcq, true or false and fill in the blank",
                      "difficulty": "Challenging"}, default_count=5, max_count=15)
    assert s["topic"] == "Photosynthesis" and s["count"] == 15
    assert s["types"] == [QT.mcq, QT.true_false, QT.fill_blank]
    assert s["difficulty"] == "hard"


def test_slot_defaults_and_garbage():
    s = coerce_slots({"count": "lots", "types": ["nonsense"], "difficulty": "any"},
                     default_count=5, max_count=15)
    assert s["count"] == 5 and "topic" not in s and s["difficulty"] == "mixed"
    assert coerce_slots({"count": 0}, default_count=5, max_count=15)["count"] == 5


@pytest.mark.parametrize("n,expect", [
    (1, {QT.mcq: 1}), (2, {QT.mcq: 2}), (3, {QT.mcq: 2, QT.true_false: 1}),
    (5, {QT.mcq: 3, QT.true_false: 1, QT.fill_blank: 1}),
    (10, {QT.mcq: 6, QT.true_false: 2, QT.fill_blank: 2}),
])
def test_default_mix_scales_with_count(n, expect):
    spec = build_spec(n, None, "mixed")
    assert {r.type: r.count for r in spec.types} == expect and spec.total_questions == n
    assert spec.explanations is True


def test_requested_types_split_evenly_and_difficulty_is_carried():
    spec = build_spec(7, [QT.matching, QT.numerical], "hard")
    assert {r.type: r.count for r in spec.types} == {QT.matching: 4, QT.numerical: 3}
    assert {r.difficulty for r in spec.types} == {"hard"}


# ── the skill ────────────────────────────────────────────────────────────────
def _quiz_json(n=3):
    return {"topic": "Sound", "curriculum_ref": {"board": "CDC", "grade": 8, "subject": "General"},
            "objectives": [{"id": "O1", "statement": "Explain that sound is a vibration",
                            "bloom": "understand"}],
            "questions": [{"id": f"x{i}", "type": "mcq", "prompt": f"Q{i}?", "options": ["a", "b", "c", "d"],
                           "answer": "a", "objective_ids": ["O1"], "explanation": "why"} for i in range(n)]}


def _ctx(skill, slots, **conv):
    return SkillContext(conversation=Conversation(**conv), message="quiz me",
                        routed=RoutedIntent("quiz", 0.9, slots, "llm"), history=[], deps=skill.deps)


def _skill(llm):
    return QuizSkill(SkillDeps(llm=llm, llm_fast=llm, grounding=None, config=ChatConfig()))


def test_skill_generates_a_quiz_artifact_with_explanations_in_the_prompt():
    llm = Script(_quiz_json(3))
    skill = _skill(llm)
    events = list(skill.run(_ctx(skill, {"topic": "sound", "count": 3, "types": ["mcq"]})))
    assert [e.type for e in events] == ["status", "artifact", "token"]
    art = events[1].data
    assert art.kind == "quiz" and art.title == "Sound quiz" and len(art.payload["questions"]) == 3
    assert art.meta["slots"]["types"] == ["mcq"]
    assert "3-question quiz" in events[2].data and "explanation" in llm.prompts[0]


def test_skill_uses_the_conversations_grade_and_subject():
    llm = Script(_quiz_json(2))
    skill = _skill(llm)
    from lessonforge.domain.chat import ChatDefaults
    list(skill.run(_ctx(skill, {"topic": "sound", "count": 2},
                        defaults=ChatDefaults(grade=6, subject="Science"))))
    assert "Grade 6 Science" in llm.prompts[0]


def test_skill_asks_for_the_topic_when_missing_and_waits():
    skill = _skill(Script(_quiz_json()))
    events = list(skill.run(_ctx(skill, {"count": 4})))
    assert [e.type for e in events] == ["token", "awaiting"]
    assert events[1].data == {"skill": "quiz", "slot": "topic", "slots": {"count": 4}}


def test_skill_apologises_instead_of_raising_when_generation_fails():
    skill = _skill(Script(RuntimeError("model down")))
    events = list(skill.run(_ctx(skill, {"topic": "sound"})))
    assert [e.type for e in events] == ["status", "token"] and "couldn't" in events[1].data


# ── the play view hides every answer ─────────────────────────────────────────
def _artifact(questions):
    from lessonforge.domain.artifacts import Quiz
    from lessonforge.domain.chat import ChatArtifact
    quiz = Quiz(topic="T", curriculum_ref={"board": "CDC", "grade": 8, "subject": "S"},
                objectives=[{"id": "O1", "statement": "Explain that sound is a vibration",
                             "bloom": "understand"}], questions=questions)
    return ChatArtifact(conversation_id="c1", kind="quiz", title="T quiz",
                        payload=quiz.model_dump(mode="json"))


ALL_TYPES = [
    _q(id="Q1", type="mcq", options=["x", "y", "z"], answer="y"),
    _q(id="Q2", type="true_false", answer="False"),
    _q(id="Q3", type="fill_blank", prompt="Sound is a _____.", answer="vibration / vibrating"),
    _q(id="Q4", type="matching", answer="", pairs=[{"left": "a", "right": "1"}, {"left": "b", "right": "2"},
                                                    {"left": "c", "right": "3"}]),
    _q(id="Q5", type="ordering", answer="", options=["first", "second", "third"]),
    _q(id="Q6", type="numerical", prompt="120 km in 4 h?", answer="30 km/h"),
    _q(id="Q7", type="short_answer", answer="because vibration", key_points=["vibration"]),
]


def test_play_view_never_contains_answers_or_explanations():
    art = _artifact(ALL_TYPES)
    pv = play_view(art)
    blob = json.dumps(pv)
    for secret in ("vibrating", "because vibration", "30 km/h", '"answer"', '"explanation"', "key_points"):
        assert secret not in blob
    by = {q["id"]: q for q in pv["questions"]}
    assert by["Q1"]["widget"] == "choice" and by["Q1"]["choices"] == ["x", "y", "z"]
    assert by["Q2"]["widget"] == "boolean" and by["Q3"]["widget"] == "text"
    assert by["Q4"]["widget"] == "match" and by["Q4"]["left"] == ["a", "b", "c"]
    assert sorted(r["letter"] for r in by["Q4"]["right"]) == ["A", "B", "C"]
    assert by["Q5"]["widget"] == "order" and len(by["Q5"]["items"]) == 3
    assert by["Q6"]["widget"] == "number" and by["Q7"]["widget"] == "written"


# ── rule grading ─────────────────────────────────────────────────────────────
G = RuleGrader()


def _grade(q, response, n=1):
    return G.grade(q, n, response)


def test_mcq_by_index_and_by_text_and_letter_keys():
    q = ALL_TYPES[0]
    assert _grade(q, 1).correct and _grade(q, "y").correct and _grade(q, "Y ").correct
    assert not _grade(q, 0).correct and not _grade(q, "garbage").correct
    assert _grade(q, 0).expected == "y"
    lettered = _q(type="mcq", options=["x", "y"], answer="B")
    assert _grade(lettered, 1).correct


def test_true_false_is_case_insensitive():
    q = ALL_TYPES[1]
    assert _grade(q, "false").correct and _grade(q, "False").correct and not _grade(q, "True").correct


def test_fill_blank_accepts_alternatives_articles_and_small_typos():
    q = ALL_TYPES[2]
    assert _grade(q, "vibration").correct and _grade(q, "Vibrating").correct
    assert _grade(q, "the vibration").correct and _grade(q, "vibraton").correct   # typo
    assert not _grade(q, "echo").correct and not _grade(q, "").correct


def test_numerical_compares_numbers_not_strings():
    q = ALL_TYPES[5]
    assert _grade(q, "30").correct and _grade(q, "30.0 km/h").correct and _grade(q, "about 30 kmph").correct
    assert not _grade(q, "40").correct and not _grade(q, "thirty").correct
    assert _grade(_q(type="numerical", answer="1,200 m"), "1200").correct


def test_matching_and_ordering_grade_against_the_shuffled_key():
    from lessonforge.domain.assessment import view
    for q in (ALL_TYPES[3], ALL_TYPES[4]):
        right = view(q, 1).answer_letters
        assert _grade(q, right).correct
        assert not _grade(q, list(reversed(right)) if len(set(right)) > 1 else ["Z"]).correct
        assert not _grade(q, "not a list").correct and not _grade(q, None).correct


def test_written_answers_are_self_check_under_rule_grading():
    g = _grade(ALL_TYPES[6], "anything")
    assert g.self_check and g.correct is None and g.expected == "because vibration"


def test_grader_never_raises_on_malformed_responses():
    for q in ALL_TYPES:
        for bad in (None, {}, [], 3.5, object()):
            G.grade(q, 1, bad)


# ── LLM grading ──────────────────────────────────────────────────────────────
def test_llm_grader_judges_written_answers_and_gives_partial_credit():
    q = ALL_TYPES[6]
    full = LLMGrader(Script({"verdict": "correct", "feedback": "Nice."})).grade(q, 1, "it vibrates")
    half = LLMGrader(Script({"verdict": "partial", "feedback": "Close."})).grade(q, 1, "it moves")
    none = LLMGrader(Script({"verdict": "incorrect", "feedback": "No."})).grade(q, 1, "magic")
    assert (full.score, full.correct, full.self_check) == (1.0, True, False) and full.feedback == "Nice."
    assert (half.score, half.correct) == (0.5, False) and none.score == 0.0


@pytest.mark.parametrize("reply", [RuntimeError("down"), "not json", {"verdict": "maybe"}])
def test_llm_grader_falls_back_to_self_check(reply):
    g = LLMGrader(Script(reply)).grade(ALL_TYPES[6], 1, "it vibrates")
    assert g.self_check and g.correct is None


def test_llm_grader_skips_the_llm_for_objective_types_and_blank_answers():
    llm = Script({"verdict": "correct"})
    gr = LLMGrader(llm)
    assert gr.grade(ALL_TYPES[0], 1, 1).correct
    assert gr.grade(ALL_TYPES[6], 1, "  ").self_check
    assert llm.prompts == []


# ── sessions: attempts, review, history, retry ───────────────────────────────
@pytest.fixture
def sess():
    store = MemoryChatStore()
    svc = QuizSessions(store, RuleGrader())
    art = store.add_artifact(_artifact(ALL_TYPES))
    return svc, art, store


def test_answering_scores_and_is_idempotent(sess):
    svc, art, _ = sess
    att = svc.start(art)
    r = svc.answer(art, att, "Q1", 1)
    assert r["correct"] and r["score"] == 1 and r["expected"] == "y" and r["explanation"] == "because"
    assert r["progress"] == {"answered": 1, "total": 7, "score": 1.0, "status": "in_progress"}
    again = svc.answer(art, att, "Q1", 0)            # changing your mind after the reveal: ignored
    assert again["correct"] and again["progress"]["answered"] == 1
    with pytest.raises(KeyError):
        svc.answer(art, att, "nope", 1)


def test_self_check_flow_holds_the_question_until_self_marked(sess):
    svc, art, _ = sess
    att = svc.start(art)
    first = svc.answer(art, att, "Q7", "my answer")
    assert first["self_check"] and first["pending"] and first["progress"]["answered"] == 0
    held = svc.answer(art, att, "Q7", "my answer")
    assert held["pending"]
    done = svc.answer(art, att, "Q7", None, self_mark=True)
    assert done["correct"] and not done["pending"] and done["progress"]["score"] == 1.0


def test_attempt_completes_when_everything_is_answered_and_review_reveals_it(sess):
    svc, art, store = sess
    att = svc.start(art)
    from lessonforge.domain.assessment import view
    qs = {q.id: q for q in ALL_TYPES}
    svc.answer(art, att, "Q1", 1)
    svc.answer(art, att, "Q2", "True")                                   # wrong
    svc.answer(art, att, "Q3", "vibration")
    svc.answer(art, att, "Q4", view(qs["Q4"], 4).answer_letters)
    svc.answer(art, att, "Q5", view(qs["Q5"], 5).answer_letters)
    svc.answer(art, att, "Q6", "30")
    last = svc.answer(art, att, "Q7", "x", self_mark=False)
    assert last["progress"]["status"] == "completed" and last["progress"]["score"] == 5.0
    rev = svc.review(art, store.get_attempt(att.id))
    assert rev["attempt"]["percent"] == 71 and rev["missed"] == ["Q2", "Q7"]
    assert rev["by_type"]["mcq"] == {"score": 1.0, "of": 1}
    assert rev["questions"][1]["expected"] == "False" and rev["questions"][1]["correct"] is False


def test_review_hides_unanswered_questions(sess):
    svc, art, store = sess
    att = svc.start(art)
    svc.answer(art, att, "Q1", 1)
    rev = svc.review(art, store.get_attempt(att.id))
    assert rev["questions"][0]["answered"] and not rev["questions"][1]["answered"]
    assert "expected" not in rev["questions"][1]


def test_finish_early_counts_the_rest_as_missed(sess):
    svc, art, store = sess
    att = svc.start(art)
    svc.answer(art, att, "Q1", 1)
    svc.finish(art, att)
    rev = svc.review(art, store.get_attempt(att.id))
    assert rev["attempt"]["status"] == "completed" and rev["attempt"]["score"] == 1.0
    assert len(rev["missed"]) == 6 and rev["questions"][1]["expected"] == "False"
    svc.finish(art, att)   # idempotent


def test_attempts_are_listed_and_summarised(sess):
    svc, art, _ = sess
    a1 = svc.start(art)
    svc.finish(art, a1)
    a2 = svc.start(art)
    s = svc.summary(art)
    assert s["attempts"] == 2 and s["best"] == 0.0 and s["in_progress"] == a2.id and s["question_count"] == 7
    listing = svc.list_attempts(art)
    assert [x["id"] for x in listing] == [a2.id, a1.id] and listing[1]["status"] == "completed"


def test_retry_missed_builds_a_new_quiz_from_only_the_wrong_ones(sess):
    svc, art, store = sess
    att = svc.start(art)
    svc.answer(art, att, "Q1", 1)           # right
    svc.answer(art, att, "Q2", "True")      # wrong
    svc.answer(art, att, "Q3", "echo")      # wrong
    new = svc.retry_missed(art, store.get_attempt(att.id))
    assert new.title == "Retry: T quiz" and new.meta["retry_of"] == art.id
    assert [q["id"] for q in new.payload["questions"]] == ["Q1", "Q2"]
    assert [q["type"] for q in new.payload["questions"]] == ["true_false", "fill_blank"]
    assert store.get_artifact(new.id) is not None
    clean = svc.start(art)
    with pytest.raises(ValueError, match="nothing was missed"):
        svc.retry_missed(art, clean)


def test_artifact_service_dispatches_by_kind_and_survives_a_bad_artifact():
    store = MemoryChatStore()
    svc = ArtifactService(store, RuleGrader())
    good = store.add_artifact(_artifact(ALL_TYPES))
    from lessonforge.domain.chat import ChatArtifact
    store.add_artifact(ChatArtifact(conversation_id="c1", kind="quiz", title="broken", payload={}))
    assert [s["id"] for s in svc.summaries("c1")] == [good.id]
    with pytest.raises(ValueError, match="unsupported artifact kind"):
        svc.for_kind("hologram")
