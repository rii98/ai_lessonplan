"""TutorRenderer — phrase the policy's ONE move in the tutor's voice.

The renderer never decides *what* to do — that came from the policy. Its prompt is
small and single-purpose (Khanmigo's lesson: long prompts get partly ignored):
persona → this move's instruction → the objective → pinned evidence → the hidden
teacher solution (for reasoning only) → a short learner note → the last few turns.

Anti-boredom rules live here too (docs §5.6): the model sees the openings of its
last replies and must not repeat them (W6), and hints are written from the student's
*actual* wrong answer, with the ladder level only bounding how much may be revealed
(W7). The pre-written hint ladder is the template fallback.

``template`` is the deterministic provider — the test seam and the fallback when the
model is down, so a session never stalls.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field

from ...domain.chat import ChatMessage, Role
from ...domain.learning import Decision, LearningSession, MoveKind, PinnedChunk
from ...providers.base import LLMClient
from .learner import uncovered


@dataclass(slots=True)
class RenderContext:
    session: LearningSession
    decision: Decision
    student_text: str = ""
    history: list[ChatMessage] = field(default_factory=list)
    ephemeral: list[PinnedChunk] = field(default_factory=list)
    learner_notes: list[str] = field(default_factory=list)   # long-term memory (personalises only)
    max_hints: int = 3
    strict: bool = False                 # a retry after the leak guard fired
    avoid: list[str] = field(default_factory=list)   # what the leak guard caught last draft
    action: str | None = None            # the student tapped a quick-action chip (no typed answer)


class TutorRenderer(ABC):
    @abstractmethod
    def render(self, ctx: RenderContext) -> str:
        """The tutor's reply text. May raise — the service falls back to templates."""


def _ideas(ctx: RenderContext, oid: str | None = None) -> list[str]:
    s = ctx.session
    obj = s.plan.objective(oid or ctx.decision.objective_id)
    open_ids = set(uncovered(s, obj.id))
    return [e.statement for e in obj.expectations if e.id in open_ids]


def _title(s: LearningSession, oid: str) -> str:
    return s.plan.objective(oid).title


def move_instruction(ctx: RenderContext) -> str:
    """The single instruction for this turn's move (+ notes)."""
    d, s = ctx.decision, ctx.session
    obj = s.plan.objective(d.objective_id)
    nxt = ", ".join(_ideas(ctx)[:1]) or obj.title
    fb = d.feedback
    lines: list[str] = []
    if fb is not None:
        verdict = {True: "got it RIGHT", False: "got it wrong", None: "wrote an answer"}[fb.correct]
        lines.append(f"The student just answered the interactive check and {verdict}"
                     + (f" (correct answer: {fb.expected})" if fb.correct is False and fb.expected else "")
                     + ". React to that first, in one short line.")
        if fb.reveal:
            lines.append(f"It was a prediction question — explain what really happens: {fb.reveal}")
        if fb.confidence is not None and fb.confidence >= 4 and fb.correct is False:
            lines.append("They were confident but wrong — treat it gently as a surprising discovery.")

    if ctx.action:
        lines.append(f"The student tapped the \"{ctx.action}\" button — they did NOT type an answer, "
                     "so don't praise or comment on an answer.")
    elif fb is None and not ctx.student_text.strip() and d.move is not MoveKind.hook:
        lines.append("The student has not replied yet.")
    m = d.move
    if m is MoveKind.hook:
        lines.append(f"Open the part \"{obj.title}\" with a short, curious real-life puzzle or "
                     f"question{f' (idea: {obj.hook})' if obj.hook else ''}. Don't explain yet. "
                     "End with ONE question.")
    elif m is MoveKind.explain_chunk:
        lines.append(f"Teach ONE small idea in 2-4 short sentences with a concrete everyday "
                     f"example: \"{nxt}\". Then ask one easy question to check it.")
    elif m is MoveKind.elicit:
        lines.append(f"Ask ONE open question that leads the student toward this idea, without "
                     f"stating it: \"{nxt}\".")
    elif m is MoveKind.pump:
        lines.append(f"Say specifically what is right in the student's answer, then ask "
                     f"\"what else?\" or \"why?\" to draw out: \"{nxt}\". Don't state it.")
    elif m is MoveKind.hint:
        lines.append(
            f"Give a hint at level {d.hint_level} of {ctx.max_hints} (1 = a gentle pointer where to "
            "look; 2 = narrow it to the specific idea; 3 = almost there, fill-in-the-blank style). "
            "Base it on what the student actually said. NEVER state the answer. End with a question."
            f" The idea they need: \"{nxt}\".")
    elif m is MoveKind.worked_step:
        lines.append("Show ONE step of the worked example (from HIDDEN) clearly, then ask the "
                     "student to try the next step themselves.")
    elif m is MoveKind.check:
        lines.append("Introduce the quick check in ONE short, upbeat sentence. Give no hints "
                     "about its answer.")
    elif m is MoveKind.confront:
        mis = next((x for x in obj.misconceptions if f"misconception:{x.id}" in d.notes), None)
        lines.append("The student shows a common misconception"
                     + (f": \"{mis.statement}\"" if mis else "") + ". Don't say \"wrong\". Give a "
                     "contrast case or a \"what would happen if…\" question that makes the conflict "
                     "visible" + (f" (use: {mis.remedy})" if mis and mis.remedy else "") + ".")
    elif m is MoveKind.affirm_and_extend:
        lines.append(f"Confirm what was right, praising their reasoning specifically (not "
                     f"\"great job\"). Then ask ONE question that leads to: \"{nxt}\".")
    elif m is MoveKind.reexplain:
        if fb is not None and fb.correct is False:
            # the card has already shown the right answer: asking it again is a non-question
            lines.append("The correct answer is already shown to the student on the card. Explain "
                         "kindly, in a NEW way (an analogy or everyday example), WHY it is right — "
                         "2-3 sentences. Do NOT ask the same question again or ask them to repeat "
                         "the answer; end with ONE short question that applies the idea to a new "
                         "case.")
        else:
            lines.append("Explain kindly, in a NEW way (an analogy or everyday example), why the "
                         "correct answer is right — 2-3 sentences. Then ask one short question.")
    elif m is MoveKind.bottom_out:
        lines.append("Give the answer clearly with the reasoning in 2-4 sentences (use HIDDEN). "
                     "No lecture about trying harder."
                     + (" Then say there's one quick question to lock it in." if d.check_id else ""))
    elif m is MoveKind.challenge:
        lines.append("Celebrate briefly, then introduce the ⭐ challenge question below as a fun "
                     "bonus in one line.")
    elif m is MoveKind.choose_next:
        a, b = (_title(s, c) for c in d.choices[:2])
        lines.append(f"Recap \"{obj.title}\" in one sentence. Then let the student choose what's "
                     f"next: \"{a}\" or \"{b}\". Ask which one.")
    elif m is MoveKind.recap_advance:
        nxt_title = _title(s, d.advance_to) if d.advance_to else ""
        lines.append(f"Recap \"{obj.title}\" in 1-2 sentences (its key ideas). Then open the next "
                     f"part \"{nxt_title}\" with a curious question or mini-puzzle.")
        if d.check_next:
            lines.append("The student already said something right about this next part earlier — "
                         "say so, and introduce a quick check below instead of a question.")
    elif m is MoveKind.answer_tangent:
        lines.append("Answer the student's question briefly (2-3 sentences) from the SOURCES and "
                     "link it back to the lesson. Then return to the current idea with one question.")
        if "steer_back" in d.notes:
            lines.append("They've had a few side questions — keep the answer to one sentence and "
                         "steer back firmly but kindly.")
    elif m is MoveKind.park:
        lines.append("The question is off-topic. Acknowledge it warmly in one sentence, say you'll "
                     "come back to it at the end, then return to the lesson with ONE question.")
    elif m is MoveKind.skip_check:
        lines.append("The student says they already know this. Say great, let's quickly check — "
                     "introduce the check below in one line.")
    elif m is MoveKind.pace_change:
        lines.append("Acknowledge going faster in one line and introduce the check below.")
    elif m is MoveKind.warm_up:
        lines.append("Welcome the student back. In one line remind them what was covered, then "
                     "introduce the quick warm-up question below.")
    elif m is MoveKind.pause:
        lines.append("Say their progress is saved and they can come back any time. 1-2 lines.")
    elif m is MoveKind.wrap_up:
        done = [o.title for o in s.plan.objectives if s.states[o.id].status in ("mastered", "skipped")]
        lines.append("Wrap up warmly. Summarise what they learned in 2-4 short bullet points"
                     + (f" (mastered: {', '.join(done)})" if done else "") + ".")
        if s.parking_lot:
            lines.append(f"Offer to talk about what they asked earlier: {'; '.join(s.parking_lot[:3])}.")

    if d.check_id:
        lines.append("An interactive check is shown right below your message. Do NOT repeat its "
                     "question or options, and do not ask a different question — end by pointing "
                     "the student to the check.")
    for n in d.notes:
        if n.startswith("noticed_ahead:"):
            oid = n.split(":", 1)[1]
            lines.append(f"The student also said something correct about a LATER part "
                         f"(\"{_title(s, oid)}\"). Acknowledge it briefly (\"you're ahead!\") and say "
                         "you'll get there.")
        elif n == "one_nudge":
            lines.append("They asked for the answer: give ONE small clue and offer one guess — warm, "
                         "no lecture, and say you'll tell them if they want.")
        elif n == "fast_track":
            lines.append("They're doing really well — keep it brisk.")
        elif n == "switch_it_up":
            lines.append("Their replies are getting short — make this playful and energetic.")
        elif n in ("moving_on", "student_moved_on"):
            lines.append("They haven't fully got this part yet. In ONE sentence give its key idea "
                         "plainly, say you'll come back to it another time, then move on — no "
                         "guilt, keep it upbeat.")
        elif n == "offer_break" and m is not MoveKind.wrap_up:
            lines.append("Mention in a few words that they can take a break any time (progress is saved).")
    if s.pace == "slow" and m in (MoveKind.explain_chunk, MoveKind.hint, MoveKind.worked_step):
        lines.append("Go slowly: very simple words, tiny steps.")
    return "\n".join(lines)


def _strict_line(ctx: RenderContext) -> str:
    said = ", ".join(f'"{a}"' for a in ctx.avoid[-3:])
    return ("IMPORTANT: your previous draft gave away the answer"
            + (f" ({said}). Do not write {said} — in words or digits — or anything that "
               "states it" if said else ". Do not state it") + ".\n")


def safe_line(ctx: RenderContext) -> str:
    """The last-resort reply when every draft leaked: tied to THIS move and part, built
    only from titles (which carry no check answers), so it still reads as a turn of the
    lesson rather than filler."""
    s, d = ctx.session, ctx.decision
    title = s.plan.objective(d.objective_id).title
    if d.check_id:
        return f"Let's check \"{title}\" — try this one:"
    if d.advance_to:
        return f"Nice — let's move on to \"{_title(s, d.advance_to)}\". What do you already know about it?"
    if d.move is MoveKind.wrap_up:
        return "Well done today — your progress is saved."
    if d.move is MoveKind.pause:
        return "Your progress is saved — come back any time!"
    return f"Let's look at \"{title}\" together. What do you already know about it?"


_PERSONA = (
    "You are a warm, encouraging tutor for a school student{who}. You teach by SCAFFOLDING: "
    "guide the student to think instead of handing over answers. Write in {lang}. Short "
    "sentences, simple words, ONE idea per reply, at most ~80 words unless the instruction asks "
    "for a summary. Ground facts in the SOURCES; never invent facts. The HIDDEN section is for "
    "your reasoning only — never reveal it unless the instruction says to give the answer. Praise "
    "reasoning, not the person. No headings. Do not start with the same words as these recent "
    "replies: {openings}."
)


class LLMRenderer(TutorRenderer):
    def __init__(self, llm: LLMClient, *, temperature: float = 0.6) -> None:
        self.llm = llm
        self.temperature = temperature

    def render(self, ctx: RenderContext) -> str:
        s, d = ctx.session, ctx.decision
        scope = s.plan.scope
        who = "".join([f" in grade {scope.grade}" if scope.grade else "",
                       f" studying {scope.subject}" if scope.subject else ""])
        lang = "Nepali (Devanagari)" if scope.language == "ne" else "simple English"
        system = _PERSONA.format(who=who, lang=lang,
                                 openings=" | ".join(s.recent_openings[-3:]) or "(none)")
        obj = s.plan.objective(d.objective_id)
        evidence = [*obj.evidence, *ctx.ephemeral]
        src = "\n".join(f"[{i}] {c.text[:700]}" for i, c in enumerate(evidence, 1)) or "(none)"
        hidden = obj.worked_example or "(none)"
        if d.check_id:
            found = s.find_check(d.check_id)
            if found:
                hidden += (f"\nThe check shown below your reply asks: {found[1].question.prompt} "
                           "— do NOT give away its answer.")
        recent = "\n".join(f"{m.role.value.capitalize()}: {m.content[:400]}"
                           for m in ctx.history[-4:] if m.role in (Role.user, Role.assistant))
        learner = "; ".join(ctx.learner_notes[:5])
        prompt = (
            f"INSTRUCTION FOR THIS REPLY:\n{move_instruction(ctx)}\n"
            + (_strict_line(ctx) if ctx.strict else "")
            + f"\nLESSON: {s.plan.goal}\nCURRENT PART: {obj.title}\n"
            f"\nSOURCES:\n{src}\n\nHIDDEN (teacher notes):\n{hidden}\n"
            + (f"\nABOUT THE LEARNER (personalise only; not facts about the subject): {learner}\n" if learner else "")
            + f"\nRECENT CONVERSATION:\n{recent or '(start)'}\n"
            + (f"Student: {ctx.student_text[:1000]}\n" if ctx.student_text else "")
            + "\nTutor:"
        )
        text = self.llm.complete(prompt, system=system, temperature=self.temperature).text.strip()
        if not text:
            raise ValueError("empty tutor reply")
        return text


class TemplateRenderer(TutorRenderer):
    """Deterministic replies per move — never leaks hidden answers (it only uses
    titles, key-idea *prompts* and the pre-written hint ladder)."""

    def render(self, ctx: RenderContext) -> str:
        s, d = ctx.session, ctx.decision
        obj = s.plan.objective(d.objective_id)
        lead = ""
        if d.feedback is not None:
            lead = {True: "Correct! ", False: "Not quite. ", None: "Thanks! "}[d.feedback.correct]
            if d.feedback.correct is False and d.feedback.expected:
                lead += f"The answer was: {d.feedback.expected}. "
            if d.feedback.reveal:
                lead += d.feedback.reveal + " "
        ahead = "".join(f" You're ahead — you already said something about "
                        f"\"{_title(s, n.split(':', 1)[1])}\"; we'll get there!"
                        for n in d.notes if n.startswith("noticed_ahead:"))
        m = d.move
        if m is MoveKind.hint:
            ladder = obj.hint_ladder
            hint = ladder[min(d.hint_level, len(ladder)) - 1] if ladder else \
                f"Think about what \"{obj.title}\" really means."
            body = f"Here's a hint: {hint} What do you think?"
        elif m is MoveKind.hook:
            body = (obj.hook or f"Let's explore: {obj.title}.") + " What's your guess?"
        elif m is MoveKind.reexplain and d.feedback is not None and d.feedback.correct is False:
            why = d.feedback.explanation or obj.worked_example.split(". ")[0]
            body = (f"{why} " if why else "") + "Can you think of another example of this?"
        elif m in (MoveKind.explain_chunk, MoveKind.reexplain):
            body = f"Let's look at it step by step. {obj.title}. Can you say it in your own words?"
        elif m is MoveKind.worked_step:
            step = obj.worked_example.split(". ")[0] if obj.worked_example else obj.title
            body = f"Here's the first step: {step}. Can you try the next step?"
        elif m is MoveKind.bottom_out:
            body = (obj.worked_example or obj.title) + (" Quick question to lock it in:" if d.check_id else "")
        elif m in (MoveKind.check, MoveKind.skip_check, MoveKind.pace_change, MoveKind.challenge,
                   MoveKind.warm_up):
            body = "Let's check — try this one:"
        elif m is MoveKind.choose_next:
            a, b = (_title(s, c) for c in d.choices[:2])
            body = f"Nice work on \"{obj.title}\"! What next: \"{a}\" or \"{b}\"?"
        elif m is MoveKind.recap_advance:
            nxt = _title(s, d.advance_to) if d.advance_to else ""
            body = f"Great — that's \"{obj.title}\" done! Next up: {nxt}." + \
                (" You mentioned this earlier — quick check:" if d.check_next else " What do you already know about it?")
        elif m is MoveKind.park:
            body = "Good question — let's save it for the end! Back to our lesson: what do you think?"
        elif m is MoveKind.answer_tangent:
            body = "Interesting question! It connects to what we're learning. Back to it — what do you think?"
        elif m is MoveKind.confront:
            body = "Hmm, interesting! What would happen if that were true? Think about it."
        elif m is MoveKind.pause:
            body = "Your progress is saved — come back any time!"
        elif m is MoveKind.wrap_up:
            done = [o.title for o in s.plan.objectives if s.states[o.id].status in ("mastered", "skipped")]
            body = "Well done! You worked through: " + (", ".join(done) or s.plan.topic) + "."
        else:   # elicit, pump, affirm
            body = f"Good thinking! What else do you know about {obj.title.lower()}?"
        return (lead + body + ahead).strip()


RENDERER_REGISTRY: dict[str, Callable[..., TutorRenderer]] = {
    "llm": lambda llm, **kw: LLMRenderer(llm, **kw) if llm else TemplateRenderer(),
    "template": lambda llm, **kw: TemplateRenderer(),
}


def build_renderer(provider: str, *, llm: LLMClient | None, temperature: float = 0.6) -> TutorRenderer:
    try:
        return RENDERER_REGISTRY[provider](llm, temperature=temperature)
    except KeyError:
        raise ValueError(f"unknown tutor renderer {provider!r}; known: {sorted(RENDERER_REGISTRY)}") from None
