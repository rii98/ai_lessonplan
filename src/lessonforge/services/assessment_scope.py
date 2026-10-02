"""Anchoring an assessment to teaching that already happened.

A standalone quiz invents its own objectives from a topic, so it can test things a
teacher never covered. When the assessment is built *from a lesson or a unit*, the
objectives are not the model's to invent: they are the ones the lessons declared. An
:class:`AssessmentScope` carries those fixed objectives (plus a human-readable note of
what the assessment covers) into the generator, which then ties every question to them.

- :func:`scope_from_lesson` — one lesson: its objectives, and its misconceptions as
  ready-made distractors / probes.
- :func:`scope_from_unit` — a whole unit (or chosen days): every day's objectives,
  re-id'd per day (``D2-O1``) because each day numbers its own from ``O1``, plus a
  per-day coverage note so questions spread across the unit instead of clustering on
  the first day.

Both return the :class:`NormalizedBrief` to ground with, so retrieval is keyed to the
lesson/unit topic, grade, subject, and framework — no retyping.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..domain.ldd import LessonDesignDocument, NormalizedBrief, Objective
from ..domain.unit import UnitDesignDocument


@dataclass(frozen=True, slots=True)
class AssessmentScope:
    objectives: list[Objective]
    focus: str = ""
    # day number → the ids of its objectives (units only; for the coverage note/UI)
    by_day: dict[int, list[str]] = field(default_factory=dict)


def _brief(ldd: LessonDesignDocument, topic: str) -> NormalizedBrief:
    r = ldd.curriculum_ref
    return NormalizedBrief(
        topic=topic, grade=r.grade, subject=r.subject, duration_min=ldd.duration_min,
        language=ldd.language, framework=ldd.framework,
    )


def scope_from_lesson(ldd: LessonDesignDocument) -> tuple[NormalizedBrief, AssessmentScope]:
    lines = [(f"This assessment checks ONE lesson: \"{ldd.topic}\". Test only what its "
              "objectives say students learned.")]
    if ldd.misconceptions:
        lines.append("Misconceptions taught in this lesson — use them as MCQ distractors and "
                     "as true/false probes:")
        lines += [f"- wrong: {m.statement}  →  right: {m.correction}" for m in ldd.misconceptions]
    return _brief(ldd, ldd.topic), AssessmentScope(
        objectives=[o.model_copy() for o in ldd.objectives], focus="\n".join(lines),
        by_day={},
    )


def scope_from_unit(
    unit: UnitDesignDocument, days: list[int] | None = None
) -> tuple[NormalizedBrief, AssessmentScope]:
    """``days`` are 1-based; ``None`` means every day. Raises ``ValueError`` on a day
    that isn't in the unit or an empty selection."""
    n = len(unit.days)
    chosen = list(range(1, n + 1)) if days is None else sorted(set(days))
    if not chosen:
        raise ValueError("select at least one day")
    bad = [d for d in chosen if d < 1 or d > n]
    if bad:
        raise ValueError(f"day(s) {bad} are not in this {n}-day unit")

    objectives: list[Objective] = []
    by_day: dict[int, list[str]] = {}
    lines = [(f"This is a UNIT-LEVEL assessment for \"{unit.title}\" covering "
              f"{'all' if len(chosen) == n else 'the selected'} {len(chosen)} day(s). Spread "
              "the questions across ALL the days below — do not cluster on the first. "
              "Some questions may connect ideas from different days.")]
    for d in chosen:
        day = unit.days[d - 1]
        ids = []
        for o in day.objectives:
            oid = f"D{d}-{o.id}"
            objectives.append(Objective(id=oid, statement=o.statement, bloom=o.bloom))
            ids.append(oid)
        by_day[d] = ids
        lines.append(f"- Day {d}: {day.topic}  [{', '.join(ids)}]")
    if unit.big_idea:
        lines.append(f"Big idea of the unit: {unit.big_idea}")
    return _brief(unit.days[chosen[0] - 1], unit.title), AssessmentScope(
        objectives=objectives, focus="\n".join(lines), by_day=by_day,
    )
