"""Learn mode units: domain guardrails, planner validation, tracer, learner model (BKT +
early credit), the scaffolding policy's rules, anchored retrieval, the leak guard and
the session store. No LLM except scripted fakes."""

from __future__ import annotations

import pytest

from lessonforge.config import LearnLearnerConfig, LearnPolicyConfig, LearnRetrievalConfig
from lessonforge.domain.learning import (
    Act,
    Affect,
    Correctness,
    Decision,
    LearningPlan,
    MoveKind,
    Objective,
    Observation,
    PinnedChunk,
    QuickAction,
    Trace,
    WidgetResult,
)
from lessonforge.services.learn.guard import RulesLeakGuard, leaks
from lessonforge.services.learn.learner import BKTLearnerModel, bkt_update
from lessonforge.services.learn.planner import LLMPlanner, build_detailed
from lessonforge.services.learn.policy import ScaffoldedPolicy, pick_check
from lessonforge.services.learn.retrieval import AnchoredRetrievalPolicy, EvidenceSource
from lessonforge.services.learn.store import MemoryLearningSessionStore, SessionConflict
from lessonforge.services.learn.tracer import LLMTracer, RulesTracer
from tests.learn_fixtures import OUTLINE, LearnLLM, detail_for, plan, session

POLICY = LearnPolicyConfig()
LEARNER = LearnLearnerConfig()


def text(act=Act.answer, correctness=Correctness.na, **kw) -> Observation:
    return Observation(text=kw.pop("said", "x"), trace=Trace(act=act, correctness=correctness, **kw))


def widget(s, check_id, correct, purpose="check", misconception=None) -> Observation:
    found = s.find_check(check_id)
    return Observation(widget=WidgetResult(check_id=check_id, objective_id=found[0].id,
                                           purpose=purpose, correct=correct, score=float(bool(correct)),
                                           misconception=misconception))


def step(s, obs):
    """learner → policy, as the service does."""
    BKTLearnerModel(LEARNER).apply(s, obs)
    return ScaffoldedPolicy(POLICY).decide(s, obs)


# ── domain ───────────────────────────────────────────────────────────────────
def test_a_detailed_objective_is_never_without_a_check():
    with pytest.raises(ValueError, match="at least one check"):
        Objective(id="O1", title="t", detailed=True,
                  expectations=[{"id": "O1.e1", "statement": "s"}], checks=[])


def test_prerequisites_must_come_earlier():
    p = plan().model_dump()
    p["objectives"][0]["prerequisites"] = ["O2"]
    with pytest.raises(ValueError, match="must come earlier"):
        LearningPlan.model_validate(p)


def test_the_public_view_hides_teaching_material():
    pub = str(session().public())
    for secret in ("energy to make food", "Plants capture sunlight", "It starts with"):
        assert secret not in pub


# ── planner validation ───────────────────────────────────────────────────────
def test_invalid_checks_are_dropped_and_a_fallback_keeps_the_objective_checkable():
    obj = plan().objectives[1].model_copy(update={"detailed": False, "checks": []})
    data = {"checks": [
        {"kind": "mcq", "prompt": "?", "options": ["a", "b"], "answer": "c", "targets": ["O2.e1"]},  # key not an option
        {"kind": "mcq", "prompt": "?", "options": ["a", "a"], "answer": "a"},                      # duplicate options
        {"kind": "telepathy", "prompt": "?"},                                                       # unknown kind
    ]}
    out = build_detailed(obj, data, [])
    assert out.detailed and [c.widget for c in out.checks] == ["explain"]


def test_check_targets_are_filtered_to_known_ids_and_kinds_normalised():
    obj = plan().objectives[1].model_copy(update={"detailed": False, "checks": []})
    data = {"checks": [{"kind": "predict", "prompt": "What happens?", "options": ["x", "y"], "answer": "y",
                        "targets": ["O9.e1", "O2.e2"], "reveal": "y because…",
                        "distractor_misconceptions": {"0": "nope"}}]}
    c = build_detailed(obj, data, []).checks[0]
    assert (c.widget, c.targets, c.reveal, c.distractor_misconceptions) == ("predict", ["O2.e2"], "y because…", {})


def test_outline_is_normalised_and_kc_refs_must_come_from_the_sources():
    class Src(EvidenceSource):
        def search(self, queries, scope, *, collections=None, top=6):
            return [PinnedChunk(text="t", metadata={"standard": "SEED-SC7-PS-1"})]

    llm = LearnLLM()
    p = LLMPlanner(llm, Src(), AnchoredRetrievalPolicy(Src(), LearnRetrievalConfig()),
                   type("C", (), {"max_objectives": 4, "checks_per_objective": 2})())
    out = p.outline("photosynthesis", plan().scope)
    assert [o.id for o in out.objectives] == ["O1", "O2"]
    assert out.objectives[1].prerequisites == ["O1"]
    assert out.objectives[0].kc_ref == "SEED-SC7-PS-1" and not out.scope.ungrounded
    assert out.objectives[0].expectations[0].id == "O1.e1"
    assert OUTLINE["objectives"][1].get("kc_ref") is None and out.objectives[1].kc_ref is None
    detailed = p.detail(out, "O2")
    assert detailed.detailed and detailed.checks[0].widget == "fill"
    assert detail_for("O2")["checks"][0]["answer"] == detailed.checks[0].question.answer


# ── tracer ───────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("said, act", [
    ("stop", Act.stop_request), ("I already know this", Act.skip_request),
    ("just tell me the answer", Act.help_request), ("idk", Act.help_request),
    ("who won the cricket match yesterday?", Act.question_off_topic),
    ("do cacti need sunlight too?", Act.question_in_scope),
    ("teach me about volcanoes", Act.switch_topic),
])
def test_rules_tracer_reads_commands_and_questions(said, act):
    assert RulesTracer().trace(session(), said, []).act is act


def test_coverage_is_scored_against_every_objective_for_early_credit():
    t = RulesTracer().trace(session(), "the sunlight energy lets it make glucose sugar", [])
    assert t.covered["O1.e1"] == 1.0 and t.covered["O3.e1"] == 1.0
    assert t.correctness is Correctness.correct


def test_llm_tracer_validates_ids_and_falls_back_on_garbage():
    s = session()
    llm = LearnLLM(trace={"act": "answer", "correctness": "correct",
                          "covered": {"O3.e1": 0.8, "O9.e9": 1}, "misconception": "made-up"})
    t = LLMTracer(llm).trace(s, "it makes some kind of food", [])
    assert t.covered == {"O3.e1": 0.8} and t.misconception is None and t.source == "llm"

    class Broken(LearnLLM):
        def complete(self, *a, **k):
            raise RuntimeError("down")
    assert LLMTracer(Broken()).trace(s, "stop", []).act is Act.stop_request


# ── learner model ────────────────────────────────────────────────────────────
def test_bkt_moves_mastery_in_the_right_direction():
    assert bkt_update(0.2, True, LEARNER) > 0.2 > bkt_update(0.2, False, LEARNER)
    assert bkt_update(0.2, True, LEARNER, 0.5) < bkt_update(0.2, True, LEARNER)


def test_early_credit_is_recorded_without_moving_the_cursor_or_mastering():
    s = session()
    obs = text(correctness=Correctness.correct, covered={"O1.e1": 0.9, "O3.e1": 0.8})
    BKTLearnerModel(LEARNER).apply(s, obs)
    assert obs.early == ["O3"] and s.cursor == "O1"
    assert s.states["O3"].early_evidence and s.states["O3"].status == "pending"
    assert s.states["O3"].p_mastery < POLICY.mastered


def test_a_wrong_distractor_records_its_misconception():
    s = session()
    BKTLearnerModel(LEARNER).apply(s, widget(s, "O1.c1", False, misconception="O1.m1"))
    assert s.state.misconceptions_seen == ["O1.m1"] and s.state.attempts == 1


# ── policy: the scaffolding rules ────────────────────────────────────────────
def test_wrong_answers_climb_the_hint_ladder_then_show_a_step_then_the_answer():
    s = session()
    moves = []
    for _ in range(6):
        d = step(s, text(correctness=Correctness.incorrect))
        moves.append((d.move, d.hint_level))
        s.states["O1"].hint_level = d.hint_level or s.states["O1"].hint_level
        if d.move is MoveKind.worked_step:
            s.states["O1"].explained = True
        s.questions_in_a_row = 0
    assert moves[:3] == [(MoveKind.hint, 1), (MoveKind.hint, 2), (MoveKind.hint, 3)]
    assert MoveKind.worked_step in [m for m, _ in moves] and moves[-1][0] is MoveKind.bottom_out


def test_a_misconception_is_confronted_not_hinted():
    s = session()
    d = step(s, text(correctness=Correctness.incorrect, misconception="O1.m1"))
    assert d.move is MoveKind.confront and "misconception:O1.m1" in d.notes


def test_partial_pumps_and_full_coverage_checks():
    s = session(detailed_all=True)
    s.cursor = "O2"
    assert step(s, text(correctness=Correctness.partial, covered={"O2.e1": 0.3})).move is MoveKind.pump
    d = step(s, text(correctness=Correctness.correct, covered={"O2.e1": 0.9}))
    assert d.move is MoveKind.affirm_and_extend              # O2.e2 still open
    d = step(s, text(correctness=Correctness.correct, covered={"O2.e2": 0.9}))
    assert d.move is MoveKind.check and d.check_id == "O2.c1"


def test_just_tell_me_gets_one_nudge_then_the_answer_with_a_transfer_check():
    s = session()
    d = step(s, text(Act.help_request, wants_answer=True))
    assert d.move is MoveKind.hint and "one_nudge" in d.notes
    s.state.hint_level = 1
    d = step(s, text(Act.help_request, wants_answer=True))
    assert d.move is MoveKind.bottom_out and d.check_id is not None


def test_a_novice_who_does_not_know_gets_taught_first_not_quizzed():
    d = step(session(), text(Act.help_request, affect=Affect.confused))
    assert d.move is MoveKind.explain_chunk


def test_off_topic_is_parked_and_in_scope_questions_are_answered_with_retrieval():
    s = session()
    d = step(s, text(Act.question_off_topic))
    assert d.move is MoveKind.park and d.retrieve == "none"
    d = step(s, text(Act.question_in_scope))
    assert d.move is MoveKind.answer_tangent and d.retrieve == "coverage_gap"
    s.state.tangents = POLICY.tangent_budget
    assert "steer_back" in step(s, text(Act.question_in_scope)).notes


def test_one_word_replies_switch_to_a_playful_widget():
    s = session()
    s.low_effort_streak = POLICY.low_effort_streak - 1
    d = step(s, text(Act.chitchat, low_effort=True))
    assert d.move is MoveKind.check and "switch_it_up" in d.notes


def test_ask_tell_balance_gives_something_after_too_many_questions():
    s = session()
    s.questions_in_a_row = POLICY.max_questions_in_a_row
    d = step(s, text(correctness=Correctness.partial))
    assert d.move is MoveKind.explain_chunk and "ask_tell_balance" in d.notes


def test_widget_cadence_and_rotation():
    s = session()
    s.turns_since_widget = POLICY.widget_every
    s.last_widget_kind = "mcq"
    d = step(s, text(Act.chitchat))
    assert d.check_id == "O1.c2" and "cadence" in d.notes     # the order widget, not mcq again
    s.last_widget_kind = "order"
    assert pick_check(s, "O1").widget == "mcq"


def test_noticing_a_later_objective_is_acknowledged():
    s = session()
    d = step(s, text(correctness=Correctness.correct, covered={"O1.e1": 0.9, "O3.e1": 0.9}))
    assert "noticed_ahead:O3" in d.notes


def test_mastery_needs_a_passed_check_then_advances():
    s = session()
    s.state.coverage["O1.e1"] = 1.0
    s.state.p_mastery = 0.9                       # high from dialogue alone…
    d = step(s, text(correctness=Correctness.correct))
    assert d.move is MoveKind.check               # …still checked
    d = step(s, widget(s, "O1.c1", True))
    assert d.move is MoveKind.recap_advance and d.advance_to == "O2" and "offer_break" in d.notes


def test_an_early_objective_opens_with_a_check_when_reached():
    s = session(prereqs=False)
    s.states["O3"].early_evidence = True
    s.state.coverage["O1.e1"] = 1.0
    s.state.p_mastery = 0.9
    d = step(s, widget(s, "O1.c1", True))
    assert (d.advance_to, d.check_next) == ("O3", True)


def test_independent_objectives_offer_a_choice_once():
    s = session(prereqs=False)
    s.state.coverage["O1.e1"] = 1.0
    s.state.p_mastery = 0.9
    d = step(s, widget(s, "O1.c1", True))
    assert d.move is MoveKind.choose_next and d.choices == ["O2", "O3"]
    s.choice_offered = True
    assert step(s, widget(s, "O1.c1", True)).move is MoveKind.recap_advance


def test_fast_learners_get_a_challenge_and_skips_are_verified():
    s = session()
    s.pace = "fast"
    s.state.coverage["O1.e1"] = 1.0
    s.state.p_mastery = 0.9
    d = step(s, widget(s, "O1.c1", True))
    assert d.move is MoveKind.challenge and d.check_purpose == "challenge"
    d = ScaffoldedPolicy(POLICY).decide(session(), Observation(action=QuickAction.skip))
    assert d.move is MoveKind.skip_check and d.check_purpose == "skip"
    s2 = session()
    assert step(s2, widget(s2, "O1.c1", True, purpose="skip")).advance_to == "O2"
    assert step(s2, widget(s2, "O1.c2", False, purpose="skip")).move is MoveKind.explain_chunk


@pytest.mark.parametrize("action, move", [
    (QuickAction.hint, MoveKind.hint), (QuickAction.example, MoveKind.worked_step),
    (QuickAction.slower, MoveKind.explain_chunk), (QuickAction.faster, MoveKind.pace_change),
    (QuickAction.pause, MoveKind.pause), (QuickAction.end, MoveKind.wrap_up),
])
def test_quick_actions_are_deterministic(action, move):
    assert ScaffoldedPolicy(POLICY).decide(session(), Observation(action=action)).move is move


def test_resume_warms_up_on_the_last_mastered_objective():
    s = session()
    s.states["O1"].status = "mastered"
    s.cursor = "O2"
    d = ScaffoldedPolicy(POLICY).resume(s)
    assert (d.move, d.objective_id, d.check_purpose) == (MoveKind.warm_up, "O1", "warm_up")


def test_finishing_the_last_objective_completes_the_session():
    s = session()
    for oid in ("O1", "O2"):
        s.states[oid].status = "mastered"
    s.cursor = "O3"
    s.states["O3"].coverage["O3.e1"] = 1.0
    s.states["O3"].p_mastery = 0.9
    d = step(s, widget(s, "O3.c1", True))
    assert d.move is MoveKind.wrap_up and d.end_status == "completed"


# ── retrieval: per objective, not per turn ───────────────────────────────────
class Spy(EvidenceSource):
    def __init__(self):
        self.calls = []

    def search(self, queries, scope, *, collections=None, top=6):
        self.calls.append((queries, collections))
        return [PinnedChunk(text="extra")]


def test_off_topic_and_ordinary_turns_never_retrieve():
    spy = Spy()
    pol = AnchoredRetrievalPolicy(spy, LearnRetrievalConfig())
    s = session()
    for move in (MoveKind.park, MoveKind.hint, MoveKind.elicit, MoveKind.check):
        assert pol.for_turn(s, Decision(move=move, objective_id="O1"), "anything") == []
    assert spy.calls == []


def test_a_coverage_gap_search_is_anchored_to_the_objective():
    spy = Spy()
    pol = AnchoredRetrievalPolicy(spy, LearnRetrievalConfig())
    d = Decision(move=MoveKind.answer_tangent, objective_id="O1", retrieve="coverage_gap")
    assert pol.for_turn(session(), d, "do cacti photosynthesise?") == [PinnedChunk(text="extra")]
    assert "Plants need sunlight" in spy.calls[0][0][0]
    spy.calls.clear()
    assert pol.for_turn(session(), d, "green plants use sunlight energy to make food") == []
    assert spy.calls == []                         # the pins already cover it


def test_repeated_failure_fetches_an_alternative_explanation():
    spy = Spy()
    s = session()
    s.state.attempts = 2
    AnchoredRetrievalPolicy(spy, LearnRetrievalConfig()).for_turn(
        s, Decision(move=MoveKind.reexplain, objective_id="O1"), "")
    assert spy.calls[0][1] == ["pedagogical", "local_context"]


# ── leak guard ───────────────────────────────────────────────────────────────
def test_the_guard_catches_an_unpassed_checks_answer_but_not_small_numbers():
    s = session()
    hint = Decision(move=MoveKind.hint, objective_id="O1")
    assert RulesLeakGuard().leaked("It's for energy to make food!", s, hint)
    assert not RulesLeakGuard().leaked("Think about the sun.", s, hint)
    assert not leaks("there are 3 steps", "3") and leaks("it is 9.8 m/s", "9.8")


def test_the_upcoming_widget_is_guarded_even_when_revealing():
    s = session()
    d = Decision(move=MoveKind.bottom_out, objective_id="O1", check_id="O1.x")
    assert not RulesLeakGuard().leaked("Plants need sunlight to make food.", s, d)
    assert RulesLeakGuard().leaked("In the dark it would stop making food.", s, d)


# ── store ────────────────────────────────────────────────────────────────────
def test_the_store_rejects_stale_writes_and_a_second_active_session():
    store = MemoryLearningSessionStore()
    s = store.create(session())
    a, b = store.get(s.id, owner_id=s.owner_id), store.get(s.id, owner_id=s.owner_id)
    store.save(a)
    with pytest.raises(SessionConflict):
        store.save(b)
    with pytest.raises(SessionConflict):
        store.create(session())
    assert store.get(s.id, owner_id="someone-else") is None
    assert store.current("c1").id == s.id


def test_the_renderer_is_told_when_a_button_was_pressed_not_an_answer_typed():
    from lessonforge.services.learn.renderer import RenderContext, move_instruction

    s = session()
    d = Decision(move=MoveKind.hint, objective_id="O1", hint_level=1)
    assert "did NOT type an answer" in move_instruction(RenderContext(session=s, decision=d, action="💡 Hint please"))
    assert "did NOT type" not in move_instruction(RenderContext(session=s, decision=d, student_text="the sun?"))


def test_a_check_attached_to_a_turn_is_introduced_not_restated():
    from lessonforge.services.learn.renderer import RenderContext, move_instruction

    d = Decision(move=MoveKind.affirm_and_extend, objective_id="O1", check_id="O1.c1")
    assert "Do NOT repeat its question" in move_instruction(RenderContext(session=session(), decision=d, student_text="x"))


# ── regression: the "stuck in a loop" report (2 checks, one missed) ──────────
def _both_checks_used_one_passed():
    """The screenshot's state: O1 has two checks; the fill-in was missed, the other
    passed, so BKT mastery (~0.6) is below 0.85 and no unused check remains."""
    s = session()
    st = s.state
    st.used_checks = ["O1.c1", "O1.c2"]
    st.checks_passed, st.attempts, st.p_mastery = 1, 0, 0.6
    st.coverage["O1.e1"] = 1.0
    return s


@pytest.mark.parametrize("obs", [
    Observation(action=QuickAction.faster),
    Observation(action=QuickAction.skip),
    text(Act.skip_request),                                   # typed "i know this nh3…"
])
def test_no_loop_when_every_check_is_used_and_one_was_passed(obs):
    d = ScaffoldedPolicy(POLICY).decide(_both_checks_used_one_passed(), obs)
    assert d.advance_to == "O2" and d.check_id is None


def test_move_on_is_honoured():
    s = session()
    t = RulesTracer().trace(s, "move on", [])
    assert t.act is Act.skip_request and t.move_on
    d = step(s, Observation(text="move on", trace=t))
    assert d.advance_to == "O2" and "student_moved_on" in d.notes


def test_a_used_check_is_never_shown_again():
    s = session()
    s.state.used_checks = ["O1.c1"]
    assert pick_check(s, "O1").id == "O1.c2"
    s.state.used_checks = ["O1.c1", "O1.c2"]
    assert pick_check(s, "O1") is None
    assert pick_check(s, "O1", allow_used=True) is not None   # spaced warm-up only


def test_exhausted_checks_with_nothing_passed_ask_for_a_fresh_one():
    s = session()
    s.state.used_checks = ["O1.c1", "O1.c2"]
    s.state.coverage["O1.e1"] = 1.0
    d = step(s, text(correctness=Correctness.correct))
    assert d.fresh_check and d.check_id is None


def test_the_turn_cap_moves_on_whatever_happens():
    s = session()
    s.state.turns = POLICY.max_turns_per_objective
    d = step(s, text(Act.help_request, affect=Affect.confused))
    assert d.advance_to == "O2" and "moving_on" in d.notes


def test_fresh_check_is_new_and_falls_back_to_explain_it_back():
    from lessonforge.services.learn.planner import fallback_check

    class Src(EvidenceSource):
        def search(self, queries, scope, *, collections=None, top=6):
            return []

    class FreshLLM(LearnLLM):
        def complete(self, prompt, *, system=None, json_schema=None, temperature=None):
            from lessonforge.providers.base import LLMResult
            if "ONE new interactive check" in (system or ""):
                return LLMResult(text='{"kind": "true_false", "prompt": "Plants make food in the dark.", '
                                      '"answer": "False", "targets": ["O1.e1"]}', raw={})
            return super().complete(prompt, system=system)

    p = plan()
    planner = LLMPlanner(FreshLLM(), Src(), AnchoredRetrievalPolicy(Src(), LearnRetrievalConfig()),
                         type("C", (), {"max_objectives": 4, "checks_per_objective": 2})())
    c = planner.fresh_check(p, "O1")
    assert c.widget == "true_false" and c.id not in {x.id for x in p.objectives[0].checks}
    p.objectives[0].checks.append(c)
    planner.llm = LearnLLM()                      # returns nothing usable → explain-back
    assert planner.fresh_check(p, "O1").id == fallback_check(p.objectives[0]).id
