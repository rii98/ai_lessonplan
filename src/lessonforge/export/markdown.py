"""Markdown renderers — deterministic, dependency-free reference layout.

These matter for two reasons:

1. **Loose coupling proof** — swapping ``lesson_plan: md`` for ``lesson_plan:
   docx`` in config changes the bytes with zero change to the service or API.
2. **Golden-file tests** — Markdown output is byte-stable for a fixed LDD (no
   timestamps, no zip nondeterminism), so ``tests/golden`` can assert exact
   bytes. DOCX/PPTX are verified structurally instead.

The lesson-plan layout reproduces ``lessonplan_reference/lp1.md``: objectives,
previous knowledge, the 5E table, closure, evaluation questions, homework.
"""

from __future__ import annotations

from ..domain.ldd import LessonDesignDocument, Question, QuestionType
from .base import ArtifactKind, RenderedArtifact, Renderer, timing_summary
from .registry import register_renderer

_MEDIA = "text/markdown; charset=utf-8"


def _encode(lines: list[str]) -> bytes:
    # trailing newline, LF endings — stable across platforms
    return ("\n".join(lines).rstrip("\n") + "\n").encode("utf-8")


def _ref_line(ldd: LessonDesignDocument) -> str:
    r = ldd.curriculum_ref
    code = f" · {r.code}" if r.code else ""
    return f"**Curriculum:** {r.board} · Grade {r.grade} · {r.subject}{code}"


def _timing_line(ldd: LessonDesignDocument) -> str:
    t = timing_summary(ldd)
    if t["balanced"]:
        flag = "✓ balanced"
    else:
        delta = t["delta_min"]
        flag = f"⚠ {'over' if delta > 0 else 'under'} by {abs(delta)} min"
    return f"**Timing:** planned {t['planned_min']} min / target {t['target_min']} min ({flag})"


@register_renderer(ArtifactKind.lesson_plan, "md")
class MarkdownLessonPlan(Renderer):
    media_type = _MEDIA
    extension = "md"

    def render(self, ldd: LessonDesignDocument) -> RenderedArtifact:
        out: list[str] = [f"# {ldd.topic}", ""]
        out += [
            _ref_line(ldd),
            (f"**Duration:** {ldd.duration_min} min · **Framework:** {ldd.framework} "
             f"· **Language:** {ldd.language}"),
            _timing_line(ldd),
            "",
        ]

        out += ["## Learning Objectives", "", "By the end of the lesson, students will be able to:", ""]
        for i, o in enumerate(ldd.objectives, 1):
            out.append(f"{i}. {o.statement} _(Bloom: {o.bloom.value})_")
        out.append("")

        if ldd.prior_knowledge:
            out += ["## Previous Knowledge", ""]
            out += [f"- {p}" for p in ldd.prior_knowledge]
            out.append("")

        out += ["## Engagement Hook", "", f"> {ldd.engagement_hook.prompt}",
                f"> — _{ldd.engagement_hook.kind}_", ""]

        if ldd.misconceptions:
            out += ["## Common Misconceptions", ""]
            for m in ldd.misconceptions:
                src = f" _(source: {m.source})_" if m.source else ""
                out.append(f"- **Misconception:** {m.statement} — **Correction:** {m.correction}{src}")
            out.append("")

        if ldd.local_context:
            out += ["## Local Context", ""]
            out += [f"- {c}" for c in ldd.local_context]
            out.append("")

        if ldd.materials:
            out += ["## Teaching Materials", ""]
            out += [f"- {m}" for m in ldd.materials]
            out.append("")

        # ── the phase table (5E / gradual release / inquiry) ────────────────
        out += [f"## {ldd.framework} Lesson Plan", "",
                "| Phase | Teacher Activities | Student Activities | Time |",
                "| --- | --- | --- | --- |"]
        for p in ldd.phases:
            label = p.name_en if not p.name_ne else f"{p.name_en} ({p.name_ne})"
            teacher = "<br>".join(f"• {a}" for a in p.teacher_activities)
            student = "<br>".join(f"• {a}" for a in p.student_activities)
            out.append(f"| {label} | {teacher} | {student} | {p.minutes} min |")
        out.append("")

        diff = ldd.differentiation
        if diff.struggling or diff.on_level or diff.advanced:
            out += ["## Differentiation", ""]
            for label, items in (("Struggling", diff.struggling),
                                 ("On level", diff.on_level),
                                 ("Advanced", diff.advanced)):
                if items:
                    out.append(f"- **{label}:** " + "; ".join(items))
            out.append("")

        if ldd.formative_checks:
            out += ["## Evaluation Questions", ""]
            for i, q in enumerate(ldd.formative_checks, 1):
                out.append(f"{i}. {q.prompt}")
            out.append("")

        if ldd.homework:
            out += ["## Homework", ""]
            out += [f"- {ins}" for ins in ldd.homework.instructions]
            out.append("")

        if ldd.quality.grounding_sources:
            out += ["## Grounding Sources", ""]
            out += [f"- {s}" for s in ldd.quality.grounding_sources]
            out.append("")

        return self._artifact(ldd, _encode(out))


def _question_block(out: list[str], q: Question, n: int) -> None:
    """Render one blank (unanswered) question. The answer key is emitted
    separately so the sheet can be printed without answers."""
    out.append(f"**{n}. {q.prompt}**")
    if q.type is QuestionType.mcq and q.options:
        out.append("")
        for j, opt in enumerate(q.options):
            out.append(f"   {chr(ord('A') + j)}. {opt}")
    elif q.type is QuestionType.true_false:
        out += ["", "   ( ) True    ( ) False"]
    else:  # short answer
        out += ["", "   _Answer: _______________________________________________"]
    out.append("")


@register_renderer(ArtifactKind.worksheet, "md")
class MarkdownWorksheet(Renderer):
    media_type = _MEDIA
    extension = "md"

    def render(self, ldd: LessonDesignDocument) -> RenderedArtifact:
        out: list[str] = [f"# Worksheet — {ldd.topic}", "",
                          _ref_line(ldd), "",
                          "**Name:** ____________________    **Date:** ____________", ""]

        out += ["## Objectives for this worksheet", ""]
        for i, o in enumerate(ldd.objectives, 1):
            out.append(f"{i}. {o.statement}")
        out.append("")

        if ldd.homework:
            out += ["## Tasks", ""]
            for i, ins in enumerate(ldd.homework.instructions, 1):
                out.append(f"{i}. {ins}")
            out.append("")

        out += ["## Practice Questions", ""]
        for i, q in enumerate(ldd.formative_checks, 1):
            _question_block(out, q, i)

        # ── answer key on its own page ──────────────────────────────────────
        out += ["---", "", "## Answer Key", ""]
        for i, q in enumerate(ldd.formative_checks, 1):
            out.append(f"{i}. {q.answer}")
        out.append("")

        return self._artifact(ldd, _encode(out))


@register_renderer(ArtifactKind.quiz, "md")
class MarkdownQuiz(Renderer):
    media_type = _MEDIA
    extension = "md"

    def render(self, ldd: LessonDesignDocument) -> RenderedArtifact:
        out: list[str] = [f"# Quiz — {ldd.topic}", "",
                          _ref_line(ldd), "",
                          ("**Name:** ____________________    **Score:** _____ / "
                           f"{len(ldd.formative_checks)}"), ""]

        for i, q in enumerate(ldd.formative_checks, 1):
            _question_block(out, q, i)

        out += ["---", "", "## Answer Key", ""]
        for i, q in enumerate(ldd.formative_checks, 1):
            out.append(f"{i}. {q.answer}")
        out.append("")

        return self._artifact(ldd, _encode(out))
