"""Interactive widgets for learn mode — inline checks inside the tutoring dialogue.

A widget kind is a thin *flavour* over the quiz :class:`Question` IR: it names which
question type it uses and how the UI should present it. Grading and the student-safe
view come from ``quiz_play`` (one grader, one "the answer never ships" rule for
quizzes and learn mode alike).

Adding a kind = one :func:`register_widget` call. The policy rotates kinds so the
student never sees the same widget twice in a row (docs §5.6 W3).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ...domain.ldd import Question, QuestionType
from ...domain.learning import CheckItem, LearningSession, WidgetResult
from ..chat.quiz_play import AnswerGrader, question_play_item


@dataclass(frozen=True, slots=True)
class WidgetKind:
    name: str
    question_type: QuestionType
    label: str                 # shown on the card ("Quick check", "Predict!")
    reveal: bool = False       # shows CheckItem.reveal after answering


WIDGET_KINDS: dict[str, WidgetKind] = {}


def register_widget(kind: WidgetKind) -> WidgetKind:
    if kind.name in WIDGET_KINDS:
        raise ValueError(f"widget kind {kind.name!r} already registered")
    WIDGET_KINDS[kind.name] = kind
    return kind


register_widget(WidgetKind("mcq", QuestionType.mcq, "Quick check"))
register_widget(WidgetKind("true_false", QuestionType.true_false, "True or false?"))
register_widget(WidgetKind("fill", QuestionType.fill_blank, "Fill the gap"))
register_widget(WidgetKind("numeric", QuestionType.numerical, "Work it out"))
register_widget(WidgetKind("order", QuestionType.ordering, "Put it in order"))
register_widget(WidgetKind("match", QuestionType.matching, "Match the pairs"))
register_widget(WidgetKind("predict", QuestionType.mcq, "Predict!", reveal=True))
register_widget(WidgetKind("explain", QuestionType.short_answer, "Explain it back"))

# which widget kinds a question type can be shown as (planner normalisation)
_DEFAULT_KIND = {
    QuestionType.mcq: "mcq", QuestionType.true_false: "true_false",
    QuestionType.fill_blank: "fill", QuestionType.numerical: "numeric",
    QuestionType.ordering: "order", QuestionType.matching: "match",
    QuestionType.short_answer: "explain", QuestionType.long_answer: "explain",
}


def kind_for(widget: str | None, question: Question) -> str:
    """A registered kind compatible with the question, preferring ``widget``."""
    k = WIDGET_KINDS.get(widget or "")
    if k is not None and k.question_type is question.type:
        return k.name
    return _DEFAULT_KIND[question.type]


class WidgetService:
    """Builds the play view of a check and grades a response to it."""

    def __init__(self, grader: AnswerGrader) -> None:
        self.grader = grader

    @staticmethod
    def play(check: CheckItem, *, purpose: str, objective_id: str,
             confidence: bool = False, instance_id: str | None = None) -> dict[str, Any]:
        """``id`` is the DISPLAY's instance id (answers are keyed by it); ``check_id`` the
        underlying check."""
        kind = WIDGET_KINDS[check.widget]
        item = question_play_item(check.question, 1)
        item.update({"id": instance_id or check.id, "check_id": check.id,
                     "kind": kind.name, "label": kind.label,
                     "purpose": purpose, "objective_id": objective_id,
                     "ask_confidence": confidence})
        if purpose == "challenge":
            item["label"] = "⭐ Challenge"
        elif purpose == "warm_up":
            item["label"] = "Warm-up"
        return item

    def grade(self, session: LearningSession, check_id: str, response: Any,
              confidence: int | None = None) -> WidgetResult | None:
        found = session.find_check(check_id)
        if found is None:
            return None
        objective, check = found
        g = self.grader.grade(check.question, 1, response)
        misconception = None
        if g.correct is False and check.question.type is QuestionType.mcq:
            try:
                misconception = check.distractor_misconceptions.get(str(int(response)))
            except (TypeError, ValueError):
                misconception = None
        purpose = session.pending.purpose if session.pending and \
            session.pending.check_id == check_id else "check"
        return WidgetResult(
            check_id=check.id, objective_id=objective.id, purpose=purpose,
            correct=g.correct, score=g.score, expected=g.expected,
            explanation=g.explanation, feedback=g.feedback,
            reveal=check.reveal if WIDGET_KINDS[check.widget].reveal else "",
            misconception=misconception, confidence=confidence,
        )
