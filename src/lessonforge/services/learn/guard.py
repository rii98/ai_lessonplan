"""AnswerLeakGuard — stop the tutor from giving the answer away (docs §5.4).

MathDial measured an unguided LLM tutor revealing the solution in ~66% of cases.
The guard is deterministic and cheap: before a reply is shown, it looks for the
answers of the checks that still *measure* something — and *always* for the answer of
the widget about to be shown under this very reply (even after a ``bottom_out``,
whose transfer check must still be a real question).

A check that has already been answered is NOT guarded: grading showed its answer on
the card, so the tutor explaining it is the point, not a leak. Answers are compared
through :mod:`lessonforge.domain.answers` — the same equivalence the grader uses, so
"three", "3" and "तीन" are one answer to both.

:meth:`AnswerLeakGuard.find` returns *what* leaked, so the service can ask the model
to rephrase without that phrase (a targeted re-ask) instead of discarding the reply.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod

from ...domain.answers import canonical, mentions
from ...domain.ldd import QuestionType
from ...domain.learning import REVEALING_MOVES, CheckItem, Decision, LearningSession


def secrets(check: CheckItem) -> list[str]:
    """The answers that would give this check away."""
    q = check.question
    out: list[str] = []
    if q.type is QuestionType.mcq:
        opts = q.options or []
        ans = q.answer.strip()
        if re.fullmatch(r"[A-Za-z]", ans) and opts:
            i = ord(ans.upper()) - ord("A")
            ans = opts[i] if 0 <= i < len(opts) else ""
        out.append(ans)
    elif q.type in (QuestionType.fill_blank, QuestionType.numerical):
        out.append(q.answer)                      # alternatives are expanded by ``mentions``
    elif q.type is QuestionType.ordering:         # the full order, as a sequence
        out.append(" ".join(q.options or []))
    # true/false, matching and written answers can't be leaked by one phrase
    return [s for s in out if canonical(s)]


def leaks(text: str, secret: str) -> bool:
    return mentions(text, secret)


def guarded_checks(session: LearningSession, decision: Decision) -> list[CheckItem]:
    """The checks whose answers must not appear in this reply."""
    guarded: list[CheckItem] = []
    if decision.check_id:
        found = session.find_check(decision.check_id)
        if found:
            guarded.append(found[1])
    if decision.move not in REVEALING_MOVES:
        obj = session.plan.objective(decision.objective_id)
        st = session.states[obj.id]
        if st.status not in ("mastered", "skipped"):
            guarded += [c for c in obj.checks
                        if c not in guarded and c.id not in st.used_checks]   # answered → shown
    return guarded


class AnswerLeakGuard(ABC):
    @abstractmethod
    def find(self, text: str, session: LearningSession, decision: Decision) -> str | None:
        """The answer this reply gives away, or ``None``."""

    def leaked(self, text: str, session: LearningSession, decision: Decision) -> bool:
        return self.find(text, session, decision) is not None


class RulesLeakGuard(AnswerLeakGuard):
    def find(self, text, session, decision):
        for c in guarded_checks(session, decision):
            for s in secrets(c):
                if leaks(text, s):
                    return s
        return None


class NoGuard(AnswerLeakGuard):
    def find(self, text, session, decision):
        return None


def build_guard(provider: str) -> AnswerLeakGuard:
    known = {"rules": RulesLeakGuard, "none": NoGuard}
    try:
        return known[provider]()
    except KeyError:
        raise ValueError(f"unknown leak guard {provider!r}; known: {sorted(known)}") from None
