"""AnswerLeakGuard — stop the tutor from giving the answer away (docs §5.4).

MathDial measured an unguided LLM tutor revealing the solution in ~66% of cases.
The guard is deterministic and cheap: before a reply is shown, it looks for the
answers of the checks the student has not passed yet — and *always* for the answer of
the widget about to be shown under this very reply (even after a ``bottom_out``,
whose transfer check must still be a real question).
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod

from ...domain.ldd import QuestionType
from ...domain.learning import REVEALING_MOVES, CheckItem, Decision, LearningSession

_NUM = re.compile(r"-?\d+(?:\.\d+)?")


def _norm(s: str) -> str:
    s = re.sub(r"[^\w\s.ऀ-ॿ]", " ", s.casefold(), flags=re.UNICODE)
    s = re.sub(r"(?<!\d)\.|\.(?!\d)", " ", s)      # keep the point only inside decimals
    return re.sub(r"\s+", " ", s).strip()


def secrets(check: CheckItem) -> list[str]:
    """The strings that would give this check away."""
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
        out += [p for p in re.split(r"\s*(?:/|\||;|\bor\b)\s*", q.answer) if p.strip()]
    elif q.type is QuestionType.ordering:   # the full order, as a sequence
        out.append(" ".join(q.options or []))
    # true/false, matching and written answers can't be leaked by one phrase
    return [s for s in out if len(_norm(s)) >= 3]


def leaks(text: str, secret: str) -> bool:
    t, s = f" {_norm(text)} ", _norm(secret)
    if not s:
        return False
    if _NUM.fullmatch(s):
        n = float(s)
        if abs(n) < 10 and "." not in s:      # small integers are everywhere in prose
            return False
        return any(float(m) == n for m in _NUM.findall(t))
    return f" {s} " in t


class AnswerLeakGuard(ABC):
    @abstractmethod
    def leaked(self, text: str, session: LearningSession, decision: Decision) -> bool: ...


class RulesLeakGuard(AnswerLeakGuard):
    def leaked(self, text, session, decision):
        guarded: list[CheckItem] = []
        if decision.check_id:
            found = session.find_check(decision.check_id)
            if found:
                guarded.append(found[1])
        if decision.move not in REVEALING_MOVES:
            obj = session.plan.objective(decision.objective_id)
            st = session.states[obj.id]
            if st.status not in ("mastered", "skipped"):
                guarded += [c for c in obj.checks if c not in guarded]
        return any(leaks(text, s) for c in guarded for s in secrets(c))


class NoGuard(AnswerLeakGuard):
    def leaked(self, text, session, decision):
        return False


def build_guard(provider: str) -> AnswerLeakGuard:
    known = {"rules": RulesLeakGuard, "none": NoGuard}
    try:
        return known[provider]()
    except KeyError:
        raise ValueError(f"unknown leak guard {provider!r}; known: {sorted(known)}") from None
