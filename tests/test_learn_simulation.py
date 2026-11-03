"""Learn mode under simulated students — the state machine tested by its INVARIANTS.

The policy is a hand-written state machine; no list of example tests can cover every
path a real student takes. So instead of one test per situation, seeded simulated
students (personas mixing right/wrong answers, numbers in other spellings, "idk",
one-word replies, off-topic and in-scope questions, quick actions, commands embedded
in questions…) play whole lessons, and after EVERY turn we assert the properties that
must hold whatever path was taken:

- the lesson always terminates (no loops), and no objective runs away;
- a check is never shown twice in a lesson (outside a warm-up);
- the reply is never empty, never the contentless filler, and never states the
  answer of the check shown right under it;
- a correct answer in another spelling is graded correct;
- a question that merely contains a command word never ends the lesson;
- the session state stays consistent (exactly one active objective while active).

A failing seed is a reproducible bug report: rerun with that seed. Each production
failure we fix also becomes a scripted case at the bottom (regressions).
"""

from __future__ import annotations

import json
import random
import re

import pytest

from lessonforge.config import LearnConfig
from lessonforge.domain.answers import mentions
from lessonforge.domain.assessment import view
from lessonforge.domain.chat import Conversation, WidgetResponse
from lessonforge.domain.ldd import QuestionType
from lessonforge.providers.base import LLMClient, LLMResult
from lessonforge.services.chat.quiz_play import RuleGrader
from lessonforge.services.learn import LearnTurn, build_learn_service
from lessonforge.services.learn.guard import secrets
from lessonforge.services.learn.planner import hook_leaks
from lessonforge.services.learn.retrieval import NullEvidenceSource
from lessonforge.services.learn.store import MemoryLearningSessionStore
from tests.learn_fixtures import OWNER

# ── a scripted planner for a real-shaped lesson (the NH3 one that broke) ─────
OUTLINE = {
    "goal": "Describe ammonia", "hook": "What makes that sharp cleaning smell?",
    "objectives": [
        {"title": "Chemical formula and structure", "prerequisites": [],
         "expectations": [{"statement": "ammonia is one nitrogen and three hydrogen atoms",
                           "keywords": ["nitrogen", "hydrogen"]}],
         "misconceptions": [{"statement": "ammonia contains oxygen", "remedy": "no O in NH3"}]},
        {"title": "Physical and chemical properties", "prerequisites": [1],
         "expectations": [{"statement": "ammonia is a gas lighter than air with a sharp smell",
                           "keywords": ["gas", "lighter", "smell"]}]},
        {"title": "Ammonia in agriculture", "prerequisites": [1],
         "expectations": [{"statement": "ammonia is used to make fertiliser",
                           "keywords": ["fertiliser", "urea"]}]},
    ],
}

DETAIL = {
    "O1": {
        # the hook states the fill check's answer — the planner must repair it
        "hook": "If you have one nitrogen atom and three hydrogen atoms, how do they stick together?",
        "worked_example": "Nitrogen shares electrons with three hydrogen atoms. The formula is NH3.",
        "hints": ["Look at the small number.", "It is after the H.", "Count: one, two, …"],
        "checks": [
            # gives itself away (NH3 in the stem) — must be dropped
            {"kind": "fill", "prompt": "Ammonia, NH3, has one nitrogen and _____ hydrogen atoms.",
             "answer": "three", "targets": ["O1.e1"]},
            {"kind": "fill", "prompt": "One ammonia molecule has _____ hydrogen atoms.",
             "answer": "three", "targets": ["O1.e1"]},
            {"kind": "mcq", "prompt": "Which element is in ammonia?",
             "options": ["oxygen", "nitrogen", "carbon"], "answer": "nitrogen",
             "targets": ["O1.e1"], "distractor_misconceptions": {"0": "O1.m1"}},
            {"kind": "true_false", "prompt": "Ammonia contains oxygen.", "answer": "False",
             "targets": ["O1.m1"]},
        ],
        "challenge": {"kind": "numeric", "prompt": "How many atoms in total in 2 molecules?",
                      "answer": "8", "targets": ["O1.e1"]},
    },
    "O2": {
        "hook": "Why does the smell spread across a room so fast?",
        "worked_example": "Ammonia is a light gas. It spreads quickly.",
        "checks": [
            {"kind": "order", "prompt": "Put in order",
             "options": ["bottle opened", "gas spreads", "we smell it"], "targets": ["O2.e1"]},
            {"kind": "mcq", "prompt": "At room temperature ammonia is a…",
             "options": ["solid", "liquid", "gas"], "answer": "gas", "targets": ["O2.e1"]},
        ],
    },
    "O3": {
        "hook": "What do farmers put on paddy fields to help them grow?",
        "worked_example": "Ammonia is turned into urea fertiliser.",
        "checks": [
            {"kind": "fill", "prompt": "Ammonia is used to make _____ for crops.",
             "answer": "fertiliser / fertilizer", "targets": ["O3.e1"]},
        ],
    },
}

FILLER = {"Let's think about it together — what do you notice?",
          "Let's think about it! What do you notice?"}


class SimLLM(LLMClient):
    """Plans with the scripted material; anything else (tutor/tracer) is unused because
    the simulation runs the deterministic rules tracer and template renderer."""

    def complete(self, prompt, *, system=None, json_schema=None, temperature=None):
        system = system or ""
        if "designing a short one-to-one tutoring session" in system:
            return LLMResult(text=json.dumps(OUTLINE), raw={})
        if "teaching material for ONE objective" in system:
            oid = re.search(r"Key ideas and misconceptions.*?\n(O\d+)\.", prompt, re.DOTALL).group(1)
            return LLMResult(text=json.dumps(DETAIL[oid]), raw={})
        if "rewrite the opening hook" in system:
            return LLMResult(text="How can tiny atoms join up to make a gas with a sharp smell?", raw={})
        if "write ONE new interactive check" in system:
            return LLMResult(text="{}", raw={})
        return LLMResult(text="{}", raw={})

    def stream(self, prompt, *, system=None, temperature=None):
        yield ""

    def health(self):
        return True


def _service():
    cfg = LearnConfig(tracer="rules", renderer="template", store="memory")
    llm = SimLLM()
    return build_learn_service(cfg, llm=llm, llm_fast=llm, grader=RuleGrader(),
                               source=NullEvidenceSource(), store=MemoryLearningSessionStore())


# ── simulated students ──────────────────────────────────────────────────────
NUMBER_SPELLINGS = {"three": ["3", "three", "Three", "३", "तीन"], "8": ["8", "eight"]}


def _right(q, rng):
    t = q.type
    if t is QuestionType.mcq:
        return next(i for i, o in enumerate(q.options) if o == q.answer)
    if t is QuestionType.true_false:
        return rng.choice([q.answer, q.answer.lower(), q.answer[0]])
    if t in (QuestionType.ordering, QuestionType.matching):
        return view(q, 1).answer_letters
    alt = q.answer.split(" / ")[0]
    return rng.choice(NUMBER_SPELLINGS.get(alt, [alt]))


def _wrong(q, rng):
    t = q.type
    if t is QuestionType.mcq:
        return next(i for i, o in enumerate(q.options) if o != q.answer)
    if t is QuestionType.true_false:
        return "True" if q.answer == "False" else "False"
    if t in (QuestionType.ordering, QuestionType.matching):
        return list(reversed(view(q, 1).answer_letters))
    return rng.choice(["4", "two", "water", ""])


TEXTS = [
    "it has nitrogen and hydrogen", "idk", "ok", "hmm", "just tell me the answer",
    "why does the reaction stop?", "who won the cricket match?", "is ammonia a gas?",
    "I already know this", "I know this is hard but can you explain it again", "move on",
    "can we do an example", "it is a gas lighter than air with a smell", "used for fertiliser",
    "no", "yes", "what's the exit route of the gas?",
]
ACTIONS = ["hint", "example", "faster", "slower", "skip"]
PERSONAS = {          # p(right answer), p(typed text instead of / between answers), p(action)
    "strong": (0.9, 0.2, 0.05), "struggling": (0.2, 0.4, 0.15),
    "chatty": (0.6, 0.7, 0.05), "clicker": (0.5, 0.1, 0.5),
}


def _play(seed: int, persona: str, max_turns: int = 120):
    rng = random.Random(seed)
    p_right, p_text, p_action = PERSONAS[persona]
    svc = _service()
    conv = Conversation(owner_id=OWNER)
    events = list(svc.begin(conv, "what's ammonia", _scope()))
    sid = next(e.data["learn"]["session_id"] for e in events if e.type == "meta")
    shown: dict[str, int] = {}
    history = []
    for turn in range(max_turns):
        s = svc.store.get(sid, owner_id=OWNER)
        assert s is not None
        _check_invariants(s, events, shown)
        if s.status in ("completed", "ended"):
            return s, turn
        widget = next((e.data for e in reversed(events) if e.type == "learn_widget"), None)
        pending = s.pending
        r = rng.random()
        if r < p_action:
            t = LearnTurn(action=rng.choice(ACTIONS))
        elif widget is not None and pending is not None and widget["id"] == pending.instance_id \
                and rng.random() > p_text:
            q = s.find_check(pending.check_id)[1].question
            resp = _right(q, rng) if rng.random() < p_right else _wrong(q, rng)
            t = LearnTurn(widget=WidgetResponse(widget_id=widget["id"], response=resp))
            events = list(svc.turn(conv, s, t, history))
            fb = next((e.data for e in events if e.type == "learn_feedback"), None)
            gradable = q.type not in (QuestionType.short_answer, QuestionType.long_answer)
            spelled = isinstance(resp, str) and resp in NUMBER_SPELLINGS.get(q.answer, [])
            if gradable and (resp == _right(q, random.Random(0)) or spelled):
                assert fb is not None and fb["correct"], (seed, q.answer, resp)
            continue
        else:
            text = rng.choice(TEXTS)
            t = LearnTurn(text=text)
            events = list(svc.turn(conv, s, t, history))
            if text.endswith("?"):
                after = svc.store.get(sid, owner_id=OWNER)
                assert after.status != "ended", f"a question ended the lesson: {text!r}"
            continue
        events = list(svc.turn(conv, s, t, history))
    raise AssertionError(f"seed {seed} ({persona}): lesson did not finish in {max_turns} turns")


def _scope():
    from lessonforge.domain.learning import PlanScope
    return PlanScope(grade=10, subject="Science")


def _check_invariants(s, events, shown):
    text = "".join(e.data for e in events if e.type == "token")
    if events and any(e.type == "learn_state" for e in events):
        assert text.strip(), "an empty tutor reply"
        assert text.strip() not in FILLER, "the contentless fallback was shown"
    for e in events:
        if e.type != "learn_widget":
            continue
        cid, purpose = e.data["check_id"], e.data["purpose"]
        if purpose != "warm_up":
            shown[cid] = shown.get(cid, 0) + 1
            assert shown[cid] == 1, f"check {cid} shown twice"
        found = s.find_check(cid)
        assert found is not None
        for secret in secrets(found[1]):
            assert not mentions(text, secret), f"reply gives away {secret!r} of {cid}: {text!r}"
    for o in s.plan.objectives:
        assert s.states[o.id].turns <= 14, f"{o.id} ran {s.states[o.id].turns} turns"
    if s.status == "active":
        active = [o.id for o in s.plan.objectives if s.states[o.id].status == "active"]
        assert active == [s.cursor], f"active objectives {active}, cursor {s.cursor}"


@pytest.mark.parametrize("persona", sorted(PERSONAS))
@pytest.mark.parametrize("seed", range(25))
def test_simulated_students_never_break_an_invariant(seed, persona):
    _play(seed, persona)


# ── regressions from real sessions (each production bug becomes a case) ─────
def test_regression_hook_and_stem_that_give_the_answer_away_are_repaired_at_authoring():
    svc = _service()
    conv = Conversation(owner_id=OWNER)
    events = list(svc.begin(conv, "what's ammonia", _scope()))
    s = svc.current(conv.id)
    o1 = s.plan.objective("O1")
    assert not hook_leaks(o1), o1.hook
    assert all("NH3" not in c.question.prompt for c in o1.checks)     # self-answering stem dropped
    text = "".join(e.data for e in events if e.type == "token")
    assert text.strip() not in FILLER and "three" not in text.lower()


def test_regression_3_for_three_is_correct_and_the_reply_does_not_reask_it():
    svc = _service()
    conv = Conversation(owner_id=OWNER)
    list(svc.begin(conv, "what's ammonia", _scope()))
    s = svc.current(conv.id)
    fill = next(c for c in s.plan.objective("O1").checks if c.question.answer == "three")
    s.pending = None
    from lessonforge.domain.learning import PendingWidget
    s.pending = PendingWidget(check_id=fill.id, objective_id="O1", purpose="check")
    s.states["O1"].used_checks.append(fill.id)
    svc.store.save(s)
    s = svc.current(conv.id)
    events = list(svc.turn(conv, s, LearnTurn(widget=WidgetResponse(
        widget_id=s.pending.instance_id, response="3")), []))
    fb = next(e.data for e in events if e.type == "learn_feedback")
    assert fb["correct"] is True


def test_regression_a_question_with_a_command_word_is_not_a_command():
    from lessonforge.domain.learning import Act
    from lessonforge.services.learn.tracer import RulesTracer
    from tests.learn_fixtures import session
    for said in ("why does the reaction stop?", "what happens next part of the cycle?",
                 "I know this is hard but can you explain it again"):
        assert RulesTracer().trace(session(), said, []).act not in (Act.stop_request, Act.skip_request), said
    assert RulesTracer().trace(session(), "stop", []).act is Act.stop_request
    assert RulesTracer().trace(session(), "ok let's move on", []).act is Act.skip_request


def test_regression_yes_no_after_a_yes_no_question_is_an_answer_not_chitchat():
    from lessonforge.domain.chat import ChatMessage, Role
    from lessonforge.domain.learning import Act
    from lessonforge.services.learn.tracer import RulesTracer
    from tests.learn_fixtures import session
    hist = [ChatMessage(conversation_id="c1", role=Role.assistant, content="Is ammonia a gas?")]
    assert RulesTracer().trace(session(), "yes", hist).act is not Act.chitchat
    assert RulesTracer().trace(session(), "ok", hist).act is Act.chitchat
