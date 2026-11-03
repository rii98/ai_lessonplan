"""StudentStateTracer — read one free-text student turn into a structured :class:`Trace`.

The tracer only *classifies* (Bridge's "identify the error" step); it never decides
what to do next. It scores coverage against **every** objective's expectations, not
just the active one, so an answer that belongs to a later objective is credited
(docs §5.1, early credit).

Providers (same loose-coupling contract as the rest of the system):

- ``rules`` — deterministic: command regexes (stop / skip / "just tell me" / "idk"),
  question detection, keyword coverage. Free, offline, the test seam and fallback.
- ``llm`` — the fast model returns strict JSON (lenient parse); ids are validated and
  rules coverage is merged in, so one odd completion can't invent progress.
- ``hybrid`` (default) — unmistakable commands and one-word replies are settled by
  rules with no LLM call; everything else goes to the LLM, falling back to rules.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import Callable

from ...domain.chat import ChatMessage, Role
from ...domain.learning import Act, Affect, Correctness, LearningSession, Trace
from ...providers.base import LLMClient
from ...util import _stem, content_terms, extract_json, overlap, words

_I = re.IGNORECASE | re.UNICODE
STOP = re.compile(r"\b(stop|quit|exit|end (?:the |this )?(?:lesson|session)|i'?m done|that'?s enough|bye)\b", _I)
SKIP = re.compile(r"\b(i (?:already )?know (?:this|that|it)(?: already)?|skip(?: this| it)?|i know already|already know)\b", _I)
MOVE_ON = re.compile(r"\b(move on|moving on|next (?:one|part|topic|question)|go next|let'?s (?:move on|continue|go on)|"
                     r"carry on|go ahead|skip (?:it|this|ahead))\b|^\s*(?:next|continue|skip)\s*[.!]?\s*$|अर्को", _I)
WANTS_ANSWER = re.compile(r"(just tell me|tell me the answer|give me the answer|what'?s the answer|"
                          r"show me the answer|just say it|उत्तर भन)", _I)
HELP = re.compile(r"(\bidk\b|\bi don'?t know\b|\bno idea\b|\bnot sure\b|\bi don'?t (?:get|understand)\b|"
                  r"\bconfus|\bhelp\b|\bhint\b|\bstuck\b|थाहा छैन|बुझिन)", _I)
SWITCH = re.compile(r"\b(?:teach me|let'?s (?:learn|study|do)|can we (?:learn|study|do)|switch to|"
                    r"i want to learn)\s+(?:about\s+)?(?P<topic>[^?.!]{3,60})", _I)
QUESTION = re.compile(r"\?\s*$|^\s*(what|why|how|when|where|which|who|does|do|is|are|can|could|will|would)\b", _I)
FRUSTRATED = re.compile(r"(this is (?:hard|boring|stupid)|i hate|ugh+|so hard|too hard|i give up|😡|😤)", _I)
LOW_EFFORT = {"ok", "k", "okay", "yes", "no", "ya", "yeah", "hmm", "fine", "sure", "nope", "yep", "kk", "ho", "hajur"}

def keyword_coverage(session: LearningSession, text: str) -> dict[str, float]:
    """Expectation id → share of its keywords present in ``text``, for every objective."""
    said = content_terms(text)
    out: dict[str, float] = {}
    for o in session.plan.objectives:
        for e in o.expectations:
            keys = {_stem(k.casefold()) for k in e.keywords if k.strip()} or content_terms(e.statement)
            if keys:
                score = len(keys & said) / len(keys)
                if score > 0:
                    out[e.id] = round(score, 2)
    return out


def _plan_vocab(session: LearningSession) -> str:
    p = session.plan
    parts = [p.topic, p.goal, *[o.title for o in p.objectives]]
    for o in p.objectives:
        parts += [e.statement for e in o.expectations] + [" ".join(e.keywords) for e in o.expectations]
    return " ".join(parts)


class StudentStateTracer(ABC):
    @abstractmethod
    def trace(self, session: LearningSession, text: str, history: list[ChatMessage]) -> Trace:
        """Classify one student turn. Must not raise."""


class RulesTracer(StudentStateTracer):
    def trace(self, session, text, history):
        return self.read(session, text)

    @staticmethod
    def command(session: LearningSession, text: str) -> Trace | None:
        """Settle unmistakable turns with no model: commands and one-word replies."""
        t = text.strip()
        ws = words(t)
        if STOP.search(t) and len(ws) <= 8:
            return Trace(act=Act.stop_request)
        if MOVE_ON.search(t) and len(ws) <= 8:
            return Trace(act=Act.skip_request, move_on=True)
        if SKIP.search(t) and len(ws) <= 10:
            return Trace(act=Act.skip_request, affect=Affect.confident)
        if WANTS_ANSWER.search(t):
            return Trace(act=Act.help_request, wants_answer=True)
        m = SWITCH.search(t)
        if m and overlap(m.group("topic"), _plan_vocab(session)) < 0.34:
            return Trace(act=Act.switch_topic, topic=m.group("topic").strip())
        if len(ws) <= 3 and HELP.search(t):
            return Trace(act=Act.help_request, affect=Affect.confused)
        if ws and len(ws) <= 2 and all(w in LOW_EFFORT for w in ws):
            return Trace(act=Act.chitchat, low_effort=True)
        return None

    def read(self, session: LearningSession, text: str) -> Trace:
        cmd = self.command(session, text)
        if cmd is not None:
            return cmd
        covered = keyword_coverage(session, text)
        affect = Affect.frustrated if FRUSTRATED.search(text) else Affect.neutral
        if HELP.search(text):
            return Trace(act=Act.help_request, affect=Affect.confused, covered=covered)
        if QUESTION.search(text):
            in_scope = overlap(text, _plan_vocab(session)) >= 0.2 or bool(covered)
            return Trace(act=Act.question_in_scope if in_scope else Act.question_off_topic,
                         affect=affect, covered=covered)
        mine = {k: v for k, v in covered.items() if k.split(".", 1)[0] == session.cursor}
        if mine and max(mine.values()) >= 0.6:
            corr = Correctness.correct
        elif mine:
            corr = Correctness.partial
        else:
            corr = Correctness.incorrect
        return Trace(act=Act.answer, correctness=corr, covered=covered, affect=affect,
                     low_effort=len(words(text)) <= 2 and not covered)


_TRACER_SYSTEM = (
    "You analyse ONE message from a school student in a tutoring session. You do not "
    "reply to the student. Output ONLY a JSON object with these keys:\n"
    '  "act": one of answer | question_in_scope | question_off_topic | help_request | '
    "skip_request | stop_request | switch_topic | chitchat\n"
    '  "correctness": correct | partial | incorrect | n/a   (n/a unless act=answer)\n'
    '  "covered": {expectation_id: 0..1} for EVERY listed key idea (from ANY objective) the '
    "message expresses, even partly, in the student's own words; omit ideas not expressed\n"
    '  "misconception": the id of a listed misconception the message shows, or null\n'
    '  "affect": neutral | confused | frustrated | bored | confident\n'
    '  "topic": for switch_topic only, the new topic; else null\n'
    "Judge meaning, not exact wording. A question about the lesson's subject is "
    "question_in_scope; anything unrelated is question_off_topic."
)

_EXAMPLE = (
    'Example — key ideas: O1.e1 "plants use sunlight for energy", O3.e1 "plants make '
    'glucose". Message: "the sun lets it make sugar" → {"act":"answer","correctness":'
    '"correct","covered":{"O1.e1":0.9,"O3.e1":0.7},"misconception":null,"affect":'
    '"neutral","topic":null}'
)


class LLMTracer(StudentStateTracer):
    def __init__(self, llm: LLMClient, fallback: StudentStateTracer | None = None) -> None:
        self.llm = llm
        self.fallback = fallback or RulesTracer()

    def trace(self, session, text, history):
        try:
            return self._llm(session, text, history)
        except Exception:
            return self.fallback.trace(session, text, history)

    def _llm(self, session: LearningSession, text: str, history: list[ChatMessage]) -> Trace:
        obj = session.objective
        ideas = []
        for o in session.plan.objectives:
            tag = " (ACTIVE)" if o.id == obj.id else ""
            ideas += [f'{e.id}{tag}: "{e.statement}"' for e in o.expectations]
            if not o.expectations:
                ideas.append(f'{o.id}: (objective "{o.title}" — not detailed yet)')
        mis = [f'{m.id}: "{m.statement}"' for m in obj.misconceptions]
        last_tutor = next((m.content for m in reversed(history) if m.role is Role.assistant), "")
        prompt = (f"Lesson topic: {session.plan.topic}\nActive objective: {obj.id} {obj.title}\n"
                  f"Key ideas:\n" + "\n".join(ideas) +
                  "\nMisconceptions:\n" + ("\n".join(mis) or "(none)") +
                  f"\n\nTutor's last message: {last_tutor[:600]}\nStudent's message: {text[:1500]}"
                  f"\n\n{_EXAMPLE}\n\nJSON:")
        data = extract_json(self.llm.complete(prompt, system=_TRACER_SYSTEM, temperature=0.0).text)
        if not isinstance(data, dict):
            raise TypeError("tracer: not an object")
        known = {e.id for o in session.plan.objectives for e in o.expectations}
        covered = {}
        for k, v in (data.get("covered") or {}).items():
            try:
                score = float(v)
            except (TypeError, ValueError):
                continue
            if k in known and score > 0:
                covered[k] = min(1.0, score)
        for k, v in keyword_coverage(session, text).items():   # rules evidence merged in
            covered[k] = max(covered.get(k, 0.0), v)
        mis_ids = {m.id for o in session.plan.objectives for m in o.misconceptions}
        misconception = data.get("misconception")
        trace = Trace(
            act=_enum(Act, data.get("act"), Act.answer),
            correctness=_enum(Correctness, data.get("correctness"), Correctness.na),
            covered=covered,
            misconception=misconception if misconception in mis_ids else None,
            affect=_enum(Affect, data.get("affect"), Affect.neutral),
            topic=(str(data["topic"])[:80] if data.get("topic") else None),
            source="llm",
        )
        if trace.act is Act.answer and trace.correctness is Correctness.na:
            trace.correctness = Correctness.partial
        trace.low_effort = len(words(text)) <= 2 and not covered
        return trace


class HybridTracer(StudentStateTracer):
    def __init__(self, llm: LLMClient) -> None:
        self.rules = RulesTracer()
        self.llm = LLMTracer(llm, fallback=self.rules)

    def trace(self, session, text, history):
        cmd = self.rules.command(session, text)
        return cmd if cmd is not None else self.llm.trace(session, text, history)


def _enum(cls, value, default):
    try:
        return cls(str(value).strip().lower())
    except ValueError:
        return default


TRACER_REGISTRY: dict[str, Callable[[LLMClient | None], StudentStateTracer]] = {
    "rules": lambda llm: RulesTracer(),
    "llm": lambda llm: LLMTracer(llm) if llm else RulesTracer(),
    "hybrid": lambda llm: HybridTracer(llm) if llm else RulesTracer(),
}


def build_tracer(provider: str, *, llm: LLMClient | None) -> StudentStateTracer:
    try:
        return TRACER_REGISTRY[provider](llm)
    except KeyError:
        raise ValueError(f"unknown tracer {provider!r}; known: {sorted(TRACER_REGISTRY)}") from None
