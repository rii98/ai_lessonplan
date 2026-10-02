"""Intent routing: the lexical gate, the rules-only and hybrid routers, multi-turn
'awaiting' slots, and thresholds. The labelled utterance sets are the regression
harness for routing quality — grow them as skills are added."""

from __future__ import annotations

import json

import pytest

from lessonforge.config import ChatIntentConfig
from lessonforge.domain.chat import ChatMessage, Role
from lessonforge.providers.base import LLMClient, LLMResult
from lessonforge.services.chat.intent import (
    HybridRouter,
    NoRouter,
    RouterState,
    RulesRouter,
    build_intent_router,
)
from lessonforge.services.chat.skills import QA, SKILL_REGISTRY
from lessonforge.services.chat.skills.quiz import SPEC as QUIZ

SPECS = [QUIZ]

# Unmistakable commands: the gate must pass AND a rules-only router must act.
COMMANDS = [
    "quiz me on photosynthesis",
    "Test me on this chapter",
    "give me 5 practice questions on fractions",
    "give me some MCQs on force",
    "make me a quiz about the water cycle",
    "create a short test on algebra",
    "ask me some questions about sound",
    "I want to practice mcqs on motion",
    "can you help me practise for the exam?",
    "check my knowledge of cells",
    "मलाई प्रश्न सोध",
]
# Words that merely MIGHT mean it: the gate passes (the LLM decides), rules don't act.
AMBIGUOUS = [
    "what questions come in the SEE science exam?",
    "is there a test on this unit?",
    "prashna",
]
# Indirect phrasings — how students actually talk. The LLM classifier is what really
# catches these; the lexical layer is the fallback when it is down, so we hold it to a
# recall floor and watch it as skills/triggers change.
INDIRECT = [
    "help me revise osmosis", "i want to revise chapter 3", "can i try some problems on fractions",
    "how well do i know the water cycle", "i have an exam tomorrow on force, help me prepare",
    "let me see if i understood photosynthesis", "can you check if i get this",
    "i'd like some exercises on algebra", "throw some mcq at me", "challenge me on cells",
    "i need to study for my science test", "give me something to solve on motion",
    "what can i do to test myself on this?", "drill me on formulas", "hit me with 10 on sound",
    "mock paper for grade 8", "malai photosynthesis ko prashna deu",
    "मलाई यो विषयमा परीक्षा दिनुहोस्", "revision questions on the heart", "worksheet on fractions",
    "i want to see how much i remember", "practice for me",
]
# Ordinary questions: no skill scores → zero extra LLM calls.
PLAIN = [
    "explain photosynthesis",
    "what is the speed of sound",
    "summarise the learning outcomes for grade 7 science",
    "why is the sky blue",
    "give me a local-context hook for teaching fractions",
    "how do plants make food",
]
# Look like quiz requests, are not — a rules-only router must not act on them.
LOOKALIKES = [
    "what is a quiz?",
    "how do I write good mcqs for my class",
    "explain what a practice test is",
]


class ScriptLLM(LLMClient):
    def __init__(self, reply=None):
        self.reply, self.calls = reply, []

    def complete(self, prompt, *, system=None, json_schema=None, temperature=None):
        self.calls.append(prompt)
        r = self.reply
        if isinstance(r, Exception):
            raise r
        return LLMResult(text=r if isinstance(r, str) else json.dumps(r), raw={})

    def health(self):
        return True


def _hybrid(reply=None, **cfg):
    """A hybrid router; the lexical gate unless a test asks otherwise (the shipped
    default is ``gate="always"`` — see ``test_default_config_classifies_every_turn``)."""
    llm = ScriptLLM(reply)
    return HybridRouter(ChatIntentConfig(**{"gate": "lexical", **cfg}), SPECS, llm), llm


# ── the lexical gate ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("text", COMMANDS)
def test_commands_score_strong(text):
    assert QUIZ.lexical_score(text) >= 0.9, text


@pytest.mark.parametrize("text", AMBIGUOUS)
def test_ambiguous_words_pass_the_gate_but_not_strongly(text):
    assert 0.3 <= QUIZ.lexical_score(text) < 0.9, text


@pytest.mark.parametrize("text", PLAIN)
def test_plain_questions_do_not_pass_the_gate(text):
    assert QUIZ.lexical_score(text) < 0.3, text


def test_lexical_fallback_keeps_a_recall_floor_on_indirect_phrasings():
    hit = [t for t in INDIRECT if QUIZ.lexical_score(t) >= 0.3]
    assert len(hit) / len(INDIRECT) >= 0.75, f"missed: {[t for t in INDIRECT if t not in hit]}"


def test_followups_only_count_right_after_the_skill_ran():
    assert QUIZ.lexical_score("harder please") < 0.3
    assert QUIZ.lexical_score("harder please", after_this_skill=True) >= 0.7


# ── rules-only router ────────────────────────────────────────────────────────
@pytest.mark.parametrize("text", COMMANDS)
def test_rules_router_acts_on_commands(text):
    r = RulesRouter(ChatIntentConfig(provider="rules"), SPECS).route(text, RouterState())
    assert r.skill == "quiz" and r.source == "rules"


@pytest.mark.parametrize("text", PLAIN + LOOKALIKES + AMBIGUOUS)
def test_rules_router_leaves_everything_else_to_qa(text):
    r = RulesRouter(ChatIntentConfig(provider="rules"), SPECS).route(text, RouterState())
    assert r.skill == QA


# ── hybrid router ────────────────────────────────────────────────────────────
@pytest.mark.parametrize("text", PLAIN)
def test_plain_questions_never_call_the_llm(text):
    router, llm = _hybrid({"intent": "quiz", "confidence": 1})
    assert router.route(text, RouterState()).skill == QA
    assert llm.calls == []          # the whole point of the gate


def test_gated_message_is_classified_and_slots_extracted():
    router, llm = _hybrid({"intent": "quiz", "confidence": 0.93, "reason": "wants practice",
                           "slots": {"topic": "photosynthesis", "count": 8, "bogus": "x", "types": []}})
    r = router.route("quiz me on photosynthesis", RouterState())
    assert (r.skill, r.source) == ("quiz", "llm")
    assert r.slots == {"topic": "photosynthesis", "count": 8}   # undeclared + empty slots dropped
    assert r.confidence == pytest.approx(0.93)
    assert len(llm.calls) == 1


def test_llm_can_overrule_the_gate_for_a_question_about_quizzes():
    router, _ = _hybrid({"intent": "qa", "confidence": 0.9, "reason": "asking what a quiz is"})
    assert router.route("what is a quiz?", RouterState()).skill == QA


def test_low_confidence_does_not_act():
    router, _ = _hybrid({"intent": "quiz", "confidence": 0.3, "slots": {"topic": "x"}})
    r = router.route("is there a test on this unit?", RouterState())
    assert r.skill == QA and "low confidence" in r.reason


def test_unknown_intent_from_the_llm_is_ignored():
    router, _ = _hybrid({"intent": "launch_missiles", "confidence": 1})
    assert router.route("quiz me on x", RouterState()).skill == QA


@pytest.mark.parametrize("bad", ["not json at all", ["a list"], RuntimeError("model down")])
def test_unusable_llm_output_falls_back_to_rules(bad):
    router, _ = _hybrid(bad)
    assert router.route("quiz me on photosynthesis", RouterState()).skill == "quiz"   # strong trigger
    assert router.route("is there a test on this unit?", RouterState()).skill == QA   # weak → don't guess


def test_classifier_prompt_is_built_from_the_skill_specs_and_context():
    router, llm = _hybrid({"intent": "qa", "confidence": 1})
    state = RouterState(history=[ChatMessage(role=Role.user, content="we covered osmosis"),
                                 ChatMessage(role=Role.assistant, content="Osmosis is diffusion.")],
                        artifacts_note="Artifacts already made in this chat:\n- quiz \"Osmosis quiz\"")
    router.route("test me on this", state)
    p = llm.calls[0]
    assert "- quiz:" in p and QUIZ.description in p
    assert 'NOT quiz: "what is a quiz?"' in p          # counter-examples sharpen the router
    assert "slots: topic" in p and "we covered osmosis" in p and "Osmosis quiz" in p


def test_default_config_classifies_every_turn_so_paraphrases_are_not_lost():
    cfg = ChatIntentConfig()
    assert cfg.gate == "always"
    llm = ScriptLLM({"intent": "quiz", "confidence": 0.9, "slots": {"topic": "osmosis"}})
    router = HybridRouter(cfg, SPECS, llm)
    # none of these contain a quiz keyword — a lexical gate would never have seen them
    for text in ("can you check if i get this", "give me something to solve on motion",
                 "i want to see how much i remember"):
        assert QUIZ.lexical_score(text) < 0.3
        assert router.route(text, RouterState()).skill == "quiz", text
    assert len(llm.calls) == 3


def test_gate_always_classifies_every_turn():
    router, llm = _hybrid({"intent": "qa", "confidence": 1}, gate="always")
    router.route("explain photosynthesis", RouterState())
    assert len(llm.calls) == 1


def test_followup_after_a_quiz_reaches_the_llm_and_routes_back():
    router, llm = _hybrid({"intent": "quiz", "confidence": 0.9, "slots": {"difficulty": "hard"}})
    assert router.route("harder", RouterState()).skill == QA and llm.calls == []   # no quiz yet
    r = router.route("harder", RouterState(last_skill="quiz"))
    assert r.skill == "quiz" and r.slots == {"difficulty": "hard"}


# ── multi-turn: awaiting a slot ──────────────────────────────────────────────
AWAITING = {"skill": "quiz", "slot": "topic", "slots": {"count": 3}}


@pytest.mark.parametrize("make", [lambda: _hybrid({"intent": "qa", "confidence": 1})[0],
                                  lambda: RulesRouter(ChatIntentConfig(), SPECS)])
def test_short_reply_fills_the_awaited_slot(make):
    r = make().route("photosynthesis", RouterState(awaiting=AWAITING))
    assert (r.skill, r.source) == ("quiz", "awaiting")
    assert r.slots == {"count": 3, "topic": "photosynthesis"}


@pytest.mark.parametrize("text", ["never mind", "cancel", "no thanks"])
def test_cancel_drops_the_pending_request(text):
    router, _ = _hybrid({"intent": "quiz", "confidence": 1})
    assert router.route(text, RouterState(awaiting=AWAITING)).skill == QA


def test_a_long_or_question_reply_is_a_new_request_not_an_answer():
    router, _llm = _hybrid({"intent": "qa", "confidence": 1})
    r = router.route("actually can you explain how the heart pumps blood around the whole body?",
                     RouterState(awaiting=AWAITING))
    assert r.source != "awaiting"
    assert router.route("what is osmosis?", RouterState(awaiting=AWAITING)).source != "awaiting"


# ── providers / registry ─────────────────────────────────────────────────────
def test_none_router_and_kill_switch():
    assert NoRouter().route("quiz me on x", RouterState()).skill == QA


def test_build_router_by_config_and_unknown_provider():
    llm = ScriptLLM()
    assert isinstance(build_intent_router(ChatIntentConfig(provider="none"), llm=llm, specs=SPECS), NoRouter)
    assert isinstance(build_intent_router(ChatIntentConfig(provider="rules"), llm=llm, specs=SPECS), RulesRouter)
    with pytest.raises(ValueError, match="Unknown intent provider"):
        build_intent_router(ChatIntentConfig(provider="telepathy"), llm=llm, specs=SPECS)


def test_quiz_skill_is_registered_and_default_is_reserved():
    assert "quiz" in SKILL_REGISTRY
    from lessonforge.services.chat.skills import Skill, SkillSpec, register_skill

    class Bad(Skill):
        spec = SkillSpec(name=QA, description="x")

        def run(self, ctx):
            yield from ()

    with pytest.raises(ValueError, match="built-in default"):
        register_skill(Bad)
