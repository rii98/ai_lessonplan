"""Shared learn-mode fixtures: a hand-built photosynthesis plan (no LLM) and a scripted
fake LLM that plays planner, tracer and tutor by system prompt."""

from __future__ import annotations

import json
import re

from lessonforge.domain.ldd import Question
from lessonforge.domain.learning import (
    CheckItem,
    Expectation,
    LearningPlan,
    LearningSession,
    Misconception,
    Objective,
    PinnedChunk,
    PlanScope,
)
from lessonforge.providers.base import LLMClient, LLMResult

OWNER = "00000000-0000-4000-8000-000000000001"


def mcq(cid, oid, prompt, options, answer, targets, distractors=None, widget="mcq"):
    return CheckItem(id=cid, widget=widget, targets=targets,
                     distractor_misconceptions=distractors or {},
                     question=Question(id=cid, type="mcq", prompt=prompt, options=options,
                                       answer=answer, objective_ids=[oid], explanation="why"))


def order(cid, oid, steps, targets):
    return CheckItem(id=cid, widget="order", targets=targets,
                     question=Question(id=cid, type="ordering", prompt="Put in order",
                                       options=steps, objective_ids=[oid]))


def plan(*, detailed_all: bool = True, prereqs: bool = True) -> LearningPlan:
    o1 = Objective(
        id="O1", title="Plants need sunlight", kc_ref="SEED-SC7-PS-1", detailed=True,
        hook="Where does a tree's mass come from?",
        expectations=[Expectation(id="O1.e1", statement="sunlight gives plants energy",
                                  keywords=["sunlight", "energy"])],
        misconceptions=[Misconception(id="O1.m1", statement="plants eat soil",
                                      remedy="soil mass barely changes")],
        worked_example="Plants capture sunlight energy in their leaves.",
        hint_ladder=["Look up at the sky.", "What shines on leaves?", "It starts with 'sun…'"],
        checks=[
            mcq("O1.c1", "O1", "Why do plants need sunlight?",
                ["to keep warm", "energy to make food", "to drink"], "energy to make food",
                ["O1.e1"], {"0": "O1.m1"}),
            order("O1.c2", "O1", ["light hits leaf", "energy captured", "food made"], ["O1.e1"]),
        ],
        challenge=mcq("O1.x", "O1", "A plant in a dark cupboard will…",
                      ["grow faster", "stop making food"], "stop making food", ["O1.e1"]),
        evidence=[PinnedChunk(text="Green plants use sunlight energy to make food.",
                              source="book", collection="reference")],
    )
    o2 = Objective(
        id="O2", title="Plants take in carbon dioxide and water",
        prerequisites=["O1"] if prereqs else [],
        expectations=[Expectation(id="O2.e1", statement="water enters through roots",
                                  keywords=["water", "roots"]),
                      Expectation(id="O2.e2", statement="carbon dioxide enters through leaves",
                                  keywords=["carbon", "dioxide", "leaves"])],
        detailed=detailed_all,
        checks=[mcq("O2.c1", "O2", "Water enters through the…", ["roots", "flowers"], "roots",
                    ["O2.e1"])] if detailed_all else [],
    )
    o3 = Objective(
        id="O3", title="Plants make glucose", prerequisites=["O2"] if prereqs else [],
        expectations=[Expectation(id="O3.e1", statement="plants make glucose sugar as food",
                                  keywords=["glucose", "sugar"])],
        detailed=detailed_all,
        checks=[mcq("O3.c1", "O3", "Plants make…", ["glucose", "salt"], "glucose", ["O3.e1"])]
        if detailed_all else [],
    )
    return LearningPlan(goal="Explain how plants make food", topic="photosynthesis",
                        scope=PlanScope(grade=7, subject="Science"), objectives=[o1, o2, o3])


def session(**plan_kw) -> LearningSession:
    s = LearningSession(conversation_id="c1", owner_id=OWNER, status="active",
                        plan=plan(**plan_kw), cursor="O1")
    s.states["O1"].status = "active"
    return s


OUTLINE = {
    "goal": "Explain how plants make food", "hook": "Where does a tree's mass come from?",
    "objectives": [
        {"title": "Plants need sunlight", "prerequisites": [], "kc_ref": "SEED-SC7-PS-1",
         "expectations": [{"statement": "sunlight gives plants energy", "keywords": ["sunlight", "energy"]}],
         "misconceptions": [{"statement": "plants eat soil", "remedy": "soil mass barely changes"}]},
        {"title": "Plants make glucose", "prerequisites": [1],
         "expectations": [{"statement": "plants make glucose sugar", "keywords": ["glucose", "sugar"]}]},
    ],
}


def detail_for(oid: str) -> dict:
    if oid == "O1":
        return {"hook": "Seeds become trees — how?", "worked_example": "Leaves capture sunlight.",
                "hints": ["Look up.", "What shines?", "sun…"],
                "checks": [{"kind": "mcq", "prompt": "Plants need sunlight for?",
                            "options": ["warmth", "energy to make food"], "answer": "energy to make food",
                            "targets": ["O1.e1"], "distractor_misconceptions": {"0": "O1.m1"}},
                           {"kind": "order", "prompt": "Order it",
                            "options": ["light hits leaf", "energy captured"], "targets": ["O1.e1"]}],
                "challenge": None}
    return {"hook": "Sweet plants?", "worked_example": "Plants make glucose.",
            "checks": [{"kind": "fill", "prompt": "Plants make _____ as food.", "answer": "glucose",
                        "targets": [f"{oid}.e1"]}]}


class LearnLLM(LLMClient):
    """Planner, tracer and tutor in one scripted fake (dispatch on the system prompt)."""

    def __init__(self, trace: dict | None = None, reply: str = "Let's think about it! What do you notice?"):
        self.trace = trace or {"act": "answer", "correctness": "partial", "covered": {}}
        self.reply = reply
        self.calls: list[str] = []

    def complete(self, prompt, *, system=None, json_schema=None, temperature=None):
        system = system or ""
        if "designing a short one-to-one tutoring session" in system:
            self.calls.append("outline")
            return LLMResult(text=json.dumps(OUTLINE), raw={})
        if "teaching material for ONE objective" in system:
            oid = re.search(r"Key ideas and misconceptions.*?\n(O\d+)\.", prompt, re.DOTALL).group(1)
            self.calls.append(f"detail:{oid}")
            return LLMResult(text=json.dumps(detail_for(oid)), raw={})
        if "analyse ONE message" in system:
            self.calls.append("trace")
            return LLMResult(text=json.dumps(self.trace), raw={})
        if "intent router" in system:
            self.calls.append("route")
            msg = prompt.split("Latest user message:")[-1].lower()
            if "teach me" in msg or "understand" in msg:
                return LLMResult(text=json.dumps({"intent": "learn", "confidence": 0.95,
                                                  "slots": {"topic": "photosynthesis"}}), raw={})
            return LLMResult(text=json.dumps({"intent": "qa", "confidence": 0.9}), raw={})
        if "tutor" in system:
            self.calls.append("tutor")
            return LLMResult(text=self.reply, raw={})
        return LLMResult(text="{}", raw={})

    def stream(self, prompt, *, system=None, temperature=None):
        yield "A plain answer."

    def health(self):
        return True
