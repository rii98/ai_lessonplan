"""LessonPlanner — compile a grounded :class:`LearningPlan` from the corpus (docs §3, §4).

Plan first, then tutor: the plan is the anchor that keeps a long, branching dialogue
on its goal. Two single-purpose LLM calls instead of one giant prompt:

1. **outline** (at session start, after ONE broad retrieval — T0): ordered objectives
   with prerequisites, key ideas (expectations + keywords), misconceptions and the
   curriculum ``kc_ref`` when a retrieved passage carries a standard code;
2. **detail** (per objective, lazily — only the first up front, the rest when the
   cursor reaches them — T1): a hook, the hidden worked example, a hint ladder,
   validated interactive checks of varied widget kinds, and a ⭐ challenge.

Everything the model returns is validated: ids are re-numbered and namespaced, check
targets filtered to known ids, each check validated through the quiz
:class:`Question` IR (MCQ answer must be an option, fill-ins need a blank…) and
dropped if invalid. If no check survives, a deterministic "explain it back" check is
synthesised, so an objective is never taught without being checked.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import Any

from ...config import LearnPlannerConfig
from ...domain.answers import mentions
from ...domain.ldd import Question, QuestionType
from ...domain.learning import (
    CheckItem,
    Expectation,
    LearningPlan,
    Misconception,
    Objective,
    PinnedChunk,
    PlanScope,
)
from ...providers.base import LLMClient
from ...util import extract_json
from .retrieval import EvidenceSource, RetrievalPolicy
from .widgets import WIDGET_KINDS, kind_for


class PlanningError(RuntimeError):
    """The planner could not produce a usable plan (the user is told plainly)."""


class LessonPlanner(ABC):
    @abstractmethod
    def outline(self, topic: str, scope: PlanScope, *, learner_notes: list[str] = ()) -> LearningPlan:
        """T0: an ordered, validated plan with undetailed objectives."""

    @abstractmethod
    def detail(self, plan: LearningPlan, objective_id: str) -> Objective:
        """T1: the objective's teaching material (returns a NEW detailed objective)."""

    @abstractmethod
    def fresh_check(self, plan: LearningPlan, objective_id: str) -> CheckItem | None:
        """One NEW validated check unlike those already asked (every planned one is used).
        ``None`` when nothing new can be made."""


_OUTLINE_SYSTEM = (
    "You are an expert teacher designing a short one-to-one tutoring session for a school "
    "student. Break the topic into {n_min}-{n} small learning objectives in teaching order "
    "(each one idea, about 3-5 minutes). Output ONLY JSON:\n"
    '{{"goal": "<one sentence: what the student will be able to do>",\n'
    ' "hook": "<a curious real-life puzzle to open the session>",\n'
    ' "objectives": [{{"title": "...", "prerequisites": [<indexes of EARLIER objectives, 1-based>],\n'
    '   "kc_ref": "<a curriculum standard code from the SOURCES if one matches, else null>",\n'
    '   "expectations": [{{"statement": "<a key idea a good answer contains>", '
    '"keywords": ["<2-4 words a student would use>"]}}],\n'
    '   "misconceptions": [{{"statement": "<a common wrong idea>", "remedy": "<the contrast/correction>"}}]}}]}}\n'
    "1-3 expectations and 0-2 misconceptions per objective. Base the content on the SOURCES "
    "when given; match the grade level."
)

_DETAIL_SYSTEM = (
    "You prepare teaching material for ONE objective of a tutoring session. Output ONLY JSON:\n"
    '{{"hook": "<a short curious question or mini-puzzle that opens this objective>",\n'
    ' "worked_example": "<the teacher solution / explanation, 3-6 short sentences, step by step>",\n'
    ' "hints": ["<gentle pointer>", "<narrower hint>", "<almost the answer>"],\n'
    ' "checks": [<{n} interactive checks, each a DIFFERENT kind>],\n'
    ' "challenge": <one harder transfer check, or null>}}\n'
    "A check is: {{\"kind\": one of {kinds}, \"prompt\": \"...\", \"targets\": [<expectation/"
    "misconception ids it tests>], ...fields by kind:\n"
    '  mcq / predict: "options": [3-4 strings], "answer": "<exactly one of the options>", '
    '"distractor_misconceptions": {{"<option index 0-based>": "<misconception id>"}}, '
    'predict also "reveal": "<what really happens and why>"\n'
    '  true_false: "answer": "True"|"False"\n'
    '  fill: prompt contains a blank "_____", "answer": "<word(s)> / <other accepted forms>"\n'
    '  numeric: "answer": "<number with unit>"\n'
    '  order: "options": [<steps in the CORRECT order>]\n'
    '  match: "pairs": [{{"left": "...", "right": "..."}}, ...3-4 pairs]\n'
    '  explain: "answer": "<model answer>", "key_points": ["..."]\n'
    'Every check has "explanation": "<one sentence why>". Pick kinds that suit the content '
    "(a process → order; term/definition → match; cause and effect → predict)."
)


def _sources_block(chunks: list[PinnedChunk]) -> str:
    out = []
    for i, c in enumerate(chunks, 1):
        std = c.metadata.get("standard")
        out.append(f"[{i}]{f' (standard: {std})' if std else ''} {c.text[:900]}")
    return "\n".join(out) or "(no sources — use your own knowledge, at the right grade level)"


def _language_rule(language: str) -> str:
    """Student-facing text must match the tutor's language (a Nepali-medium student
    must not get English checks); keywords carry both so coverage works either way."""
    if language == "ne":
        return ("Language: write ALL student-facing text (titles, statements, hooks, checks, "
                "options, hints) in Nepali (Devanagari). Give keywords in BOTH Nepali and English.")
    return "Language: write all student-facing text in simple English."


def _str_list(v: Any, limit: int) -> list[str]:
    if not isinstance(v, list):
        return []
    return [str(x).strip()[:300] for x in v if str(x).strip()][:limit]


class LLMPlanner(LessonPlanner):
    def __init__(self, llm: LLMClient, source: EvidenceSource, retrieval: RetrievalPolicy,
                 cfg: LearnPlannerConfig) -> None:
        self.llm, self.source, self.retrieval, self.cfg = llm, source, retrieval, cfg

    # ── T0 ───────────────────────────────────────────────────────────────────
    def outline(self, topic, scope, *, learner_notes=()):
        evidence = self.source.search(
            [topic, f"{topic} key concepts explained", f"{topic} common misconceptions"], scope, top=8)
        n = self.cfg.max_objectives
        system = _OUTLINE_SYSTEM.format(n=n, n_min=min(2, n))
        level = " ".join(x for x in [f"Grade {scope.grade}" if scope.grade else "",
                                     scope.subject or ""] if x)
        prompt = (f"Topic: {topic}\n{f'Level: {level}' if level else ''}\n{_language_rule(scope.language)}\n"
                  + (f"About the learner: {'; '.join(learner_notes)}\n" if learner_notes else "")
                  + f"\nSOURCES:\n{_sources_block(evidence)}\n\nJSON:")
        try:
            data = extract_json(self.llm.complete(prompt, system=system, temperature=0.2).text)
        except Exception as exc:
            raise PlanningError("the planner did not return a plan") from exc
        standards = {str(c.metadata["standard"]) for c in evidence if c.metadata.get("standard")}
        try:
            plan = self._outline_from(data, topic, scope, standards)
        except Exception as exc:
            raise PlanningError(f"the plan was not usable: {exc}") from exc
        plan.scope.ungrounded = not evidence
        if evidence:   # T0 pins for the first objective come from the broad retrieval
            plan.objectives[0].evidence = evidence[: self.retrieval_pins]
        return plan

    @property
    def retrieval_pins(self) -> int:
        return getattr(getattr(self.retrieval, "cfg", None), "pins_per_objective", 3)

    def _outline_from(self, data: Any, topic: str, scope: PlanScope, standards: set[str]) -> LearningPlan:
        if not isinstance(data, dict):
            raise TypeError("not an object")
        raw = [o for o in (data.get("objectives") or []) if isinstance(o, dict) and o.get("title")]
        raw = raw[: self.cfg.max_objectives]
        if not raw:
            raise ValueError("no objectives")
        objectives: list[Objective] = []
        for i, o in enumerate(raw, 1):
            oid = f"O{i}"
            prereq = []
            for p in o.get("prerequisites") or []:
                try:
                    k = int(str(p).lstrip("Oo"))
                except ValueError:
                    continue
                if 1 <= k < i:
                    prereq.append(f"O{k}")
            exps = []
            for j, e in enumerate([e for e in o.get("expectations") or [] if isinstance(e, dict)
                                   and e.get("statement")][:3], 1):
                exps.append(Expectation(id=f"{oid}.e{j}", statement=str(e["statement"])[:300],
                                        keywords=_str_list(e.get("keywords"), 6)))
            if not exps:   # a title alone still gives one checkable idea
                exps.append(Expectation(id=f"{oid}.e1", statement=str(o["title"])[:300]))
            mis = [Misconception(id=f"{oid}.m{j}", statement=str(m["statement"])[:300],
                                 remedy=str(m.get("remedy") or "")[:400])
                   for j, m in enumerate([m for m in o.get("misconceptions") or []
                                          if isinstance(m, dict) and m.get("statement")][:2], 1)]
            kc = o.get("kc_ref")
            objectives.append(Objective(
                id=oid, title=str(o["title"])[:160], prerequisites=sorted(set(prereq)),
                kc_ref=str(kc) if kc and str(kc) in standards else None,
                expectations=exps, misconceptions=mis))
        return LearningPlan(goal=str(data.get("goal") or f"Understand {topic}")[:300],
                            topic=topic[:160], scope=scope.model_copy(),
                            hook=str(data.get("hook") or "")[:400], objectives=objectives)

    # ── T1 ───────────────────────────────────────────────────────────────────
    def detail(self, plan, objective_id):
        obj = plan.objective(objective_id)
        if obj.detailed:
            return obj
        pins = obj.evidence or self.retrieval.pins_for(
            plan.topic, plan.scope, obj.title, [e.statement for e in obj.expectations])
        ids = [f'{e.id}: "{e.statement}"' for e in obj.expectations] + \
              [f'{m.id}: "{m.statement}" (misconception)' for m in obj.misconceptions]
        system = _DETAIL_SYSTEM.format(n=self.cfg.checks_per_objective,
                                       kinds=" | ".join(sorted(WIDGET_KINDS)))
        prompt = (f"Session goal: {plan.goal}\nObjective: {obj.title}\n{_language_rule(plan.scope.language)}\n"
                  f"{f'Grade {plan.scope.grade}' if plan.scope.grade else ''}\n"
                  f"Key ideas and misconceptions (use these ids in targets):\n" + "\n".join(ids) +
                  f"\n\nSOURCES:\n{_sources_block(pins)}\n\nJSON:")
        data: Any = {}
        try:
            data = extract_json(self.llm.complete(prompt, system=system, temperature=0.3).text)
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}
        detailed = build_detailed(obj, data, pins)
        leaked = hook_leaks(detailed)
        if leaked:
            detailed = detailed.model_copy(update={"hook": self._repair_hook(detailed, leaked)})
        return detailed

    def _repair_hook(self, obj: Objective, leaked: list[str]) -> str:
        """Validate-and-repair: one targeted rewrite of a hook that states a check's
        answer, re-validated; a neutral opener from the title if that fails too."""
        avoid = ", ".join(f'"{a}"' for a in leaked)
        prompt = (f"Objective: {obj.title}\nHook: {obj.hook}\n\nThis hook gives away {avoid}. "
                  f"Rewrite it without {avoid} (in words or digits).")
        try:
            text = self.llm.complete(prompt, system=_REPAIR_HOOK_SYSTEM,
                                     temperature=0.4).text.strip().strip('"')[:400]
        except Exception:
            text = ""
        plausible = len(text) >= 12 and not text.lstrip().startswith(("{", "[")) and "?" in text
        if plausible and not hook_leaks(obj.model_copy(update={"hook": text})):
            return text
        return neutral_hook(obj)

    def fresh_check(self, plan, objective_id):
        obj = plan.objective(objective_id)
        known = {e.id for e in obj.expectations} | {m.id for m in obj.misconceptions}
        system = _FRESH_SYSTEM.replace("{kinds}", " | ".join(sorted(WIDGET_KINDS))).replace("{{", "{").replace("}}", "}")
        try:
            data = extract_json(self.llm.complete(_fresh_prompt(plan, obj), system=system,
                                                  temperature=0.5).text)
        except Exception:
            data = None
        asked = {c.question.prompt.strip().casefold() for c in obj.checks}
        check = _check(obj, data, f"{obj.id}.f{len(obj.checks) + 1}", known) if isinstance(data, dict) else None
        if check is not None and check.question.prompt.strip().casefold() not in asked \
                and not gives_itself_away(check):
            return check
        fallback = fallback_check(obj)       # "explain it in your own words", if not used yet
        return None if any(c.id == fallback.id for c in obj.checks) else fallback


_REPAIR_HOOK_SYSTEM = (
    "You rewrite the opening hook of a tutoring step. The hook must make the student curious "
    "WITHOUT stating the answer they are about to work out. Output ONLY the new hook: one "
    "short question, plain text."
)

_FRESH_SYSTEM = (
    "You write ONE new interactive check for an objective of a tutoring session. It must test "
    "the same idea in a DIFFERENT way from the checks already asked (a new example, a "
    "different kind). Output ONLY the check as JSON, in this format:\n"
) + _DETAIL_SYSTEM.split("A check is:", 1)[1]


def _fresh_prompt(plan: LearningPlan, obj: Objective) -> str:
    ids = [f'{e.id}: "{e.statement}"' for e in obj.expectations] + \
          [f'{m.id}: "{m.statement}" (misconception)' for m in obj.misconceptions]
    asked = "\n".join(f"- {c.question.prompt}" for c in obj.checks) or "(none)"
    return (f"Objective: {obj.title}\n{_language_rule(plan.scope.language)}\n"
            f"Key ideas (use these ids in targets):\n" + "\n".join(ids) +
            f"\nAlready asked — do NOT repeat:\n{asked}\n\nJSON:")


# ── validation (pure; unit-tested without an LLM) ────────────────────────────
def build_detailed(obj: Objective, data: dict[str, Any], pins: list[PinnedChunk]) -> Objective:
    known = {e.id for e in obj.expectations} | {m.id for m in obj.misconceptions}
    checks: list[CheckItem] = []
    for i, raw in enumerate(data.get("checks") or [], 1):
        c = _check(obj, raw, f"{obj.id}.c{i}", known)
        if c is not None and not gives_itself_away(c):
            checks.append(c)
    if not checks:
        checks.append(fallback_check(obj))
    challenge = _check(obj, data.get("challenge"), f"{obj.id}.x", known) \
        if isinstance(data.get("challenge"), dict) else None
    if challenge is not None and gives_itself_away(challenge):
        challenge = None
    hints = _str_list(data.get("hints"), 4)
    # every hint but the last ("almost the answer") must leave the answer to the student
    hints = [h for i, h in enumerate(hints)
             if i == len(hints) - 1 or not _states_answer(h, checks)]
    return obj.model_copy(update={
        "hook": str(data.get("hook") or obj.hook or "")[:400],
        "worked_example": str(data.get("worked_example") or "")[:1500]
                          or " ".join(e.statement for e in obj.expectations),
        "hint_ladder": hints,
        "checks": checks, "challenge": challenge, "evidence": pins, "detailed": True,
    })


# ── item quality (authoring time; strict — a false alarm costs one rewrite) ──
_ANSWERABLE = (QuestionType.fill_blank, QuestionType.numerical)


def _answers(c: CheckItem) -> list[str]:
    q = c.question
    if q.type in _ANSWERABLE or (q.type is QuestionType.mcq and q.answer):
        return [q.answer]
    return []


def gives_itself_away(c: CheckItem) -> bool:
    """A fill/numeric check whose prompt states its answer ("…NH3… one nitrogen and _____
    hydrogen atoms" → three) measures reading, not understanding."""
    q = c.question
    return q.type in _ANSWERABLE and mentions(q.prompt.replace("_____", " "), q.answer, strict=True)


def _states_answer(text: str, checks: list[CheckItem]) -> list[str]:
    return [a for c in checks for a in _answers(c) if mentions(text, a, strict=True)]


def hook_leaks(obj: Objective) -> list[str]:
    """The check answers the objective's hook gives away."""
    return _states_answer(obj.hook, obj.checks) if obj.hook else []


def neutral_hook(obj: Objective) -> str:
    return f"Let's explore \"{obj.title}\". What do you already know about it?"


def fallback_check(obj: Objective) -> CheckItem:
    """Always valid: explain the objective back in your own words."""
    q = Question(id=f"{obj.id}.c0", type=QuestionType.short_answer, objective_ids=[obj.id],
                 prompt=f"In your own words: {obj.title}?",
                 answer=" ".join(e.statement for e in obj.expectations),
                 key_points=[e.statement for e in obj.expectations],
                 explanation=obj.expectations[0].statement if obj.expectations else "")
    return CheckItem(id=q.id, widget="explain", question=q,
                     targets=[e.id for e in obj.expectations])


_QTYPE = {"mcq": "mcq", "predict": "mcq", "true_false": "true_false", "fill": "fill_blank",
          "numeric": "numerical", "order": "ordering", "match": "matching", "explain": "short_answer"}


def _check(obj: Objective, raw: Any, cid: str, known: set[str]) -> CheckItem | None:
    if not isinstance(raw, dict) or not raw.get("prompt"):
        return None
    kind = str(raw.get("kind") or "mcq").strip().lower()
    qtype = _QTYPE.get(kind)
    if qtype is None:
        return None
    payload: dict[str, Any] = {
        "id": cid, "type": qtype, "prompt": str(raw["prompt"])[:600],
        "answer": str(raw.get("answer") or "")[:400], "objective_ids": [obj.id],
        "explanation": str(raw.get("explanation") or "")[:400],
        "key_points": _str_list(raw.get("key_points"), 5),
    }
    if qtype in ("mcq", "ordering"):
        payload["options"] = _str_list(raw.get("options"), 6)
    if qtype == "matching":
        payload["pairs"] = [{"left": str(p.get("left"))[:200], "right": str(p.get("right"))[:200]}
                            for p in raw.get("pairs") or [] if isinstance(p, dict)
                            and p.get("left") and p.get("right")][:5]
    if qtype == "fill_blank" and "__" not in payload["prompt"]:
        payload["prompt"] = re.sub(r"\.{3,}|…", "_____", payload["prompt"])
    if qtype == "true_false":
        payload["answer"] = "True" if payload["answer"].strip().lower() in ("true", "t", "yes") else "False"
    try:
        q = Question.model_validate(payload)
    except Exception:
        return None
    if q.type is QuestionType.mcq:
        opts = [o.strip().casefold() for o in q.options or []]
        if len(set(opts)) != len(opts) or len(opts) < 2:
            return None                          # duplicate / too few options
        ans = q.answer.strip()
        if ans.casefold() not in opts and not (re.fullmatch(r"[A-Da-d]", ans)
                                               and ord(ans.upper()) - 65 < len(opts)):
            return None                          # the key must be one of the options
    targets = [t for t in raw.get("targets") or [] if t in known] or \
        [e.id for e in obj.expectations[:1]]
    distractors = {}
    for k, v in (raw.get("distractor_misconceptions") or {}).items():
        if v in known and str(k).isdigit():
            distractors[str(int(k))] = v
    return CheckItem(id=cid, widget=kind_for(kind if kind in WIDGET_KINDS else None, q),
                     question=q, targets=targets, distractor_misconceptions=distractors,
                     reveal=str(raw.get("reveal") or "")[:500])


def build_planner(cfg: LearnPlannerConfig, *, llm: LLMClient, source: EvidenceSource,
                  retrieval: RetrievalPolicy) -> LessonPlanner:
    if cfg.provider != "llm":
        raise ValueError(f"unknown lesson planner {cfg.provider!r}; known: ['llm']")
    return LLMPlanner(llm, source, retrieval, cfg)
