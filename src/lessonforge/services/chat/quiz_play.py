"""Playing a quiz: the student-safe view, server-side grading, attempts, history.

A quiz artifact's payload is the same :class:`~lessonforge.domain.artifacts.Quiz` IR
the print exports use — one source of truth, so the quiz a student plays can also be
downloaded as a docx/pptx by a teacher with no conversion.

Design points:

- **The play view carries no answers.** :func:`play_view` strips answers, explanations
  and marking points; the browser only learns the right answer for a question *after*
  it has answered it, from the grader. That also makes attempts authoritative (the
  server scored them), which is what a teacher-assigned quiz or progress analytics
  will need later.
- **Grading is a port** (:class:`AnswerGrader`). :class:`RuleGrader` is deterministic
  for the objective types; :class:`LLMGrader` adds a judged verdict for typed short/
  long answers, falling back to *self-check* ("I got it / not quite") if the model is
  unavailable — a quiz never blocks on the LLM.
- **Attempts are rows** (:class:`ArtifactAttempt`): many per quiz, so it can be
  retaken and the history revisited; each stores the per-question verdicts.
"""

from __future__ import annotations

import difflib
import re
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from ...domain.artifacts import Quiz
from ...domain.assessment import view
from ...domain.chat import ArtifactAttempt, ChatArtifact
from ...domain.ldd import Question, QuestionType
from ...providers.base import LLMClient
from ...util import extract_json
from .store import ChatStore

KIND = "quiz"
WRITTEN = (QuestionType.short_answer, QuestionType.long_answer)

# which input widget the UI shows for each type
_WIDGET = {
    QuestionType.mcq: "choice", QuestionType.true_false: "boolean",
    QuestionType.fill_blank: "text", QuestionType.numerical: "number",
    QuestionType.matching: "match", QuestionType.ordering: "order",
    QuestionType.short_answer: "written", QuestionType.long_answer: "written",
}


def _now() -> datetime:
    return datetime.now(UTC)


def _split_letter(s: str) -> dict[str, str]:
    letter, _, text = s.partition(". ")
    return {"letter": letter, "text": text}


# ── the student-safe view ────────────────────────────────────────────────────
def play_view(artifact: ChatArtifact) -> dict[str, Any]:
    """The quiz as the student sees it: prompts and choices, never answers."""
    quiz = Quiz.model_validate(artifact.payload)
    items = []
    for n, q in enumerate(quiz.questions, 1):
        v = view(q, n)
        item: dict[str, Any] = {
            "id": q.id, "number": n, "type": q.type.value, "widget": _WIDGET[q.type],
            "prompt": q.prompt, "difficulty": v.difficulty,
        }
        if q.type is QuestionType.mcq:
            item["choices"] = list(q.options or [])
        elif q.type is QuestionType.matching:
            item["left"] = [p.left for p in (q.pairs or [])]
            item["right"] = [_split_letter(r) for r in v.right]
        elif q.type is QuestionType.ordering:
            item["items"] = [_split_letter(c) for c in v.choices]
        items.append(item)
    return {"id": artifact.id, "kind": KIND, "title": artifact.title, "topic": quiz.topic,
            "sources": quiz.grounding_sources, "notes": quiz.notes, "questions": items}


# ── grading ──────────────────────────────────────────────────────────────────
@dataclass(slots=True)
class Grade:
    correct: bool | None            # None → needs the student's self-mark
    score: float                    # 0..1 (0.5 = partly right)
    expected: str
    explanation: str = ""
    key_points: list[str] = field(default_factory=list)
    feedback: str = ""
    self_check: bool = False


_ARTICLES = re.compile(r"^(a|an|the)\s+", re.IGNORECASE)


def _norm(s: Any) -> str:
    s = str(s).strip().casefold()
    s = re.sub(r"[^\w\s.%/-]", " ", s, flags=re.UNICODE)
    s = _ARTICLES.sub("", re.sub(r"\s+", " ", s).strip())
    return s


def _alternatives(answer: str) -> list[str]:
    """``"vibrates / vibrating"`` → both are right."""
    parts = [p for p in re.split(r"\s*(?:/|\||;|\bor\b)\s*", answer) if p.strip()]
    return parts or [answer]


def _text_match(response: Any, answer: str) -> bool:
    r = _norm(response)
    if not r:
        return False
    for alt in _alternatives(answer):
        a = _norm(alt)
        if r == a:
            return True
        if len(a) >= 5 and difflib.SequenceMatcher(None, r, a).ratio() >= 0.88:
            return True  # a small typo shouldn't cost the mark
    return False


_NUM = re.compile(r"-?\d[\d,]*(?:\.\d+)?")


def _first_number(s: Any) -> float | None:
    m = _NUM.search(str(s))
    if not m:
        return None
    try:
        return float(m.group().replace(",", ""))
    except ValueError:
        return None


class AnswerGrader(ABC):
    @abstractmethod
    def grade(self, q: Question, number: int, response: Any) -> Grade:
        """Grade one response. Must not raise for a malformed response — that is a
        wrong answer, not an error."""


class RuleGrader(AnswerGrader):
    """Deterministic grading for every type that has a checkable answer; written
    answers become self-check (see :class:`LLMGrader` for judged ones)."""

    def grade(self, q: Question, number: int, response: Any) -> Grade:
        base = {"explanation": q.explanation, "key_points": list(q.key_points)}
        t = q.type
        try:
            if t is QuestionType.mcq:
                return self._mcq(q, response, base)
            if t is QuestionType.true_false:
                ok = _norm(response) == _norm(q.answer)
                return Grade(ok, float(ok), q.answer, **base)
            if t is QuestionType.fill_blank:
                ok = _text_match(response, q.answer)
                return Grade(ok, float(ok), q.answer, **base)
            if t is QuestionType.numerical:
                return self._numerical(q, response, base)
            if t in (QuestionType.matching, QuestionType.ordering):
                v = view(q, number)
                ok = isinstance(response, list) and [str(x) for x in response] == v.answer_letters
                return Grade(ok, float(ok), v.answer, **base)
        except Exception:
            return Grade(False, 0.0, q.answer, **base)
        return Grade(None, 0.0, q.answer, self_check=True, **base)   # written

    @staticmethod
    def _mcq(q: Question, response: Any, base: dict[str, Any]) -> Grade:
        opts = q.options or []
        want = next((i for i, o in enumerate(opts) if _norm(o) == _norm(q.answer)), None)
        if want is None and re.fullmatch(r"[A-Za-z]", q.answer.strip()):  # key given as a letter
            want = ord(q.answer.strip().upper()) - ord("A")
        try:
            got = int(response)
        except (TypeError, ValueError):
            got = next((i for i, o in enumerate(opts) if _norm(o) == _norm(response)), -1)
        ok = want is not None and got == want
        expected = opts[want] if want is not None and 0 <= want < len(opts) else q.answer
        return Grade(ok, float(ok), expected, **base)

    @staticmethod
    def _numerical(q: Question, response: Any, base: dict[str, Any]) -> Grade:
        want, got = _first_number(q.answer), _first_number(response)
        if want is None:                       # an answer with no number: compare as text
            ok = _text_match(response, q.answer)
        else:
            ok = got is not None and abs(got - want) <= max(abs(want) * 0.01, 1e-9)
        return Grade(ok, float(ok), q.answer, **base)


_JUDGE_SYSTEM = (
    "You mark a school student's written answer against a model answer and marking "
    "points. Be fair: reward correct ideas in the student's own words; do not require "
    "the model wording. Output ONLY JSON: {\"verdict\": \"correct\"|\"partial\"|"
    "\"incorrect\", \"feedback\": <one short, kind sentence to the student>}."
)


class LLMGrader(RuleGrader):
    """Rule grading, plus an LLM-judged verdict for typed written answers. Any
    failure leaves the question as self-check — grading never blocks the quiz."""

    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm

    def grade(self, q: Question, number: int, response: Any) -> Grade:
        g = super().grade(q, number, response)
        if q.type not in WRITTEN or not str(response or "").strip():
            return g
        pts = "\n".join(f"- {p}" for p in q.key_points) or "(none)"
        prompt = (f"Question: {q.prompt}\nModel answer: {q.answer}\nMarking points:\n{pts}\n\n"
                  f"Student's answer: {str(response)[:1500]}\n\nJSON:")
        try:
            data = extract_json(self.llm.complete(prompt, system=_JUDGE_SYSTEM,
                                                  temperature=0.0).text)
            verdict = str(data.get("verdict", "")).lower()
            score = {"correct": 1.0, "partial": 0.5, "incorrect": 0.0}[verdict]
        except Exception:
            return g
        g.correct = verdict == "correct"
        g.score, g.self_check = score, False
        g.feedback = str(data.get("feedback", ""))[:300]
        return g


# ── sessions ─────────────────────────────────────────────────────────────────
class QuizSessions:
    """Attempts over quiz artifacts: start, answer, finish, review, retry-missed."""

    def __init__(self, store: ChatStore, grader: AnswerGrader) -> None:
        self.store, self.grader = store, grader

    # ── reading ──────────────────────────────────────────────────────────────
    def summary(self, artifact: ChatArtifact) -> dict[str, Any]:
        attempts = self.store.list_attempts(artifact.id)
        done = [a for a in attempts if a.status == "completed"]
        n = len(self._quiz(artifact).questions)   # validates: a corrupt payload raises
        return {
            "id": artifact.id, "kind": KIND, "title": artifact.title,
            "message_id": artifact.message_id, "created_at": artifact.created_at.isoformat(),
            "question_count": n,
            "attempts": len(attempts),
            "best": max((a.score for a in done), default=None),
            "last": done[0].score if done else None,
            "in_progress": next((a.id for a in attempts if a.status == "in_progress"), None),
        }

    @staticmethod
    def _quiz(artifact: ChatArtifact) -> Quiz:
        return Quiz.model_validate(artifact.payload)

    # ── attempts ─────────────────────────────────────────────────────────────
    def start(self, artifact: ChatArtifact) -> ArtifactAttempt:
        n = len(self._quiz(artifact).questions)
        return self.store.save_attempt(ArtifactAttempt(
            artifact_id=artifact.id, conversation_id=artifact.conversation_id, kind=KIND,
            total=n, state={"answers": {}}))

    def answer(self, artifact: ChatArtifact, attempt: ArtifactAttempt, question_id: str,
               response: Any = None, self_mark: bool | None = None) -> dict[str, Any]:
        """Grade one question. Idempotent: an already-final answer is returned as
        stored. A self-check question is held ``pending`` until it is self-marked."""
        quiz = self._quiz(artifact)
        idx = next((i for i, q in enumerate(quiz.questions) if q.id == question_id), None)
        if idx is None:
            raise KeyError(question_id)
        q = quiz.questions[idx]
        answers: dict[str, Any] = attempt.state.setdefault("answers", {})
        rec = answers.get(question_id)
        if rec and not rec.get("pending"):
            return self._public(rec, attempt)
        if rec and rec.get("pending") and self_mark is not None:   # the self-mark arrives
            rec.update(correct=bool(self_mark), score=1.0 if self_mark else 0.0, pending=False)
        elif rec and rec.get("pending"):
            return self._public(rec, attempt)
        else:
            g = self.grader.grade(q, idx + 1, response)
            rec = {"response": response, "answered_at": _now().isoformat(), **asdict(g),
                   "pending": g.self_check}
            if g.self_check and self_mark is not None:             # one-shot self-mark
                rec.update(correct=bool(self_mark), score=1.0 if self_mark else 0.0,
                           pending=False)
            answers[question_id] = rec
        self._refresh(attempt, quiz)
        self.store.save_attempt(attempt)
        return self._public(rec, attempt)

    @staticmethod
    def _refresh(attempt: ArtifactAttempt, quiz: Quiz) -> None:
        recs = attempt.state.get("answers", {})
        final = [r for r in recs.values() if not r.get("pending")]
        attempt.score = round(sum(r.get("score", 0.0) for r in final), 2)
        attempt.total = len(quiz.questions)
        attempt.updated_at = _now()
        if len(final) >= len(quiz.questions):
            attempt.status = "completed"

    @staticmethod
    def _public(rec: dict[str, Any], attempt: ArtifactAttempt) -> dict[str, Any]:
        keys = ("correct", "score", "expected", "explanation", "key_points", "feedback",
                "self_check", "pending")
        return {**{k: rec.get(k) for k in keys},
                "progress": {"answered": sum(1 for r in attempt.state["answers"].values()
                                             if not r.get("pending")),
                             "total": attempt.total, "score": attempt.score,
                             "status": attempt.status}}

    def finish(self, artifact: ChatArtifact, attempt: ArtifactAttempt) -> ArtifactAttempt:
        """End early: unanswered questions count as wrong. Idempotent."""
        if attempt.status != "completed":
            quiz = self._quiz(artifact)
            answers = attempt.state.setdefault("answers", {})
            for n, q in enumerate(quiz.questions, 1):
                rec = answers.get(q.id)
                if rec is None or rec.get("pending"):
                    g = self.grader.grade(q, n, None)
                    answers[q.id] = {"response": (rec or {}).get("response"),
                                     "answered_at": _now().isoformat(), **asdict(g),
                                     "correct": False, "score": 0.0, "pending": False,
                                     "skipped": rec is None}
            attempt.status = "completed"
            self._refresh(attempt, quiz)
            attempt.status = "completed"
            self.store.save_attempt(attempt)
        return attempt

    # ── review / history ─────────────────────────────────────────────────────
    def review(self, artifact: ChatArtifact, attempt: ArtifactAttempt) -> dict[str, Any]:
        """The attempt with every answered question revealed — for the results screen
        and for revisiting an old attempt. Unanswered questions stay hidden."""
        quiz = self._quiz(artifact)
        recs = attempt.state.get("answers", {})
        rows, by_type = [], {}
        for n, q in enumerate(quiz.questions, 1):
            rec = recs.get(q.id)
            row: dict[str, Any] = {"id": q.id, "number": n, "type": q.type.value,
                                   "prompt": q.prompt, "answered": bool(rec and not rec.get("pending"))}
            if row["answered"]:
                row.update({k: rec.get(k) for k in ("response", "correct", "score", "expected",
                                                    "explanation", "key_points", "feedback")})
                got, of = by_type.get(q.type.value, (0.0, 0))
                by_type[q.type.value] = (got + rec.get("score", 0.0), of + 1)
            rows.append(row)
        total = len(quiz.questions)
        return {
            "attempt": {"id": attempt.id, "status": attempt.status, "score": attempt.score,
                        "total": total, "percent": round(100 * attempt.score / total) if total else 0,
                        "created_at": attempt.created_at.isoformat(),
                        "updated_at": attempt.updated_at.isoformat()},
            "by_type": {t: {"score": s, "of": o} for t, (s, o) in by_type.items()},
            "questions": rows,
            "missed": [r["id"] for r in rows if r["answered"] and (r.get("score") or 0) < 1],
        }

    def list_attempts(self, artifact: ChatArtifact) -> list[dict[str, Any]]:
        total = len(self._quiz(artifact).questions)
        return [{"id": a.id, "status": a.status, "score": a.score, "total": total,
                 "answered": sum(1 for r in a.state.get("answers", {}).values()
                                 if not r.get("pending")),
                 "created_at": a.created_at.isoformat(), "updated_at": a.updated_at.isoformat()}
                for a in self.store.list_attempts(artifact.id)]

    # ── retry what was missed ────────────────────────────────────────────────
    def retry_missed(self, artifact: ChatArtifact, attempt: ArtifactAttempt) -> ChatArtifact:
        """A NEW quiz made of only the questions this attempt got wrong — pure data
        reshuffling, no LLM. Raises ``ValueError`` when nothing was missed."""
        quiz = self._quiz(artifact)
        recs = attempt.state.get("answers", {})
        missed = [q for q in quiz.questions
                  if q.id in recs and (recs[q.id].get("score") or 0) < 1
                  and not recs[q.id].get("pending")]
        if not missed:
            raise ValueError("nothing was missed — there is nothing to retry")
        missed = [q.model_copy(update={"id": f"Q{i}"}, deep=True) for i, q in enumerate(missed, 1)]
        retry = quiz.model_copy(update={"questions": missed, "spec": None, "notes": []})
        art = ChatArtifact(
            conversation_id=artifact.conversation_id, message_id=artifact.message_id,
            kind=KIND, title=f"Retry: {artifact.title}",
            payload=retry.model_dump(mode="json"),
            meta={"retry_of": artifact.id, "from_attempt": attempt.id})
        return self.store.add_artifact(art)
