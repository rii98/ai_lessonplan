"""DOCX renderers (python-docx).

The lesson-plan renderer reproduces the ``lp1.md`` layout: framed header,
objectives, the 5E table (Phase / Teacher / Student / Time), closure, evaluation
questions, and homework. Worksheet and quiz renderers share the question layout
with an answer key on its own page.

**Devanagari de-risking (M3):** every run's fonts are set explicitly, including
the *complex-script* slot (``w:cs``). Word uses that slot to shape Devanagari, so
bilingual text like ``Engage (संलग्न गराउनु)`` renders with the configured
Devanagari font rather than tofu boxes. :func:`iter_devanagari` /
``tests/test_export_docx`` assert the codepoints and the font hint survive into
the document XML.
"""

from __future__ import annotations

import io

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor
from docx.text.run import Run

from ..domain.artifacts import Quiz, Worksheet
from ..domain.ldd import LessonDesignDocument, Question, QuestionType
from ..rag.grounding import ensure_sources
from .base import ArtifactKind, ExportOptions, RenderedArtifact, Renderer, timing_summary
from .registry import register_renderer
from .richtext import parse_inline

_MEDIA = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_MONO = "Consolas"


def _style_run(run: Run, opts: ExportOptions, *, mono: bool = False) -> None:
    """Set Latin *and* complex-script fonts on a run so Devanagari shapes.
    Inline code uses a monospace face for the Latin slots but keeps the
    Devanagari font on the complex-script slot."""
    face = _MONO if mono else opts.body_font
    run.font.name = face
    rpr = run._element.get_or_add_rPr()
    rfonts = rpr.get_or_add_rFonts()
    rfonts.set(qn("w:ascii"), face)
    rfonts.set(qn("w:hAnsi"), face)
    # complex-script slot → Devanagari-capable font
    rfonts.set(qn("w:cs"), opts.devanagari_font)


def _run(paragraph, text: str, opts: ExportOptions, *, bold: bool = False,
         italic: bool = False, size: int | None = None,
         color: RGBColor | None = None) -> Run:
    """Append ``text`` to ``paragraph`` as one or more styled runs.

    Inline Markdown (``**bold**``, ``*italic*``, ``` `code` ```) and LaTeX math
    ($…$, $$…$$, \\(..\\), \\[..\\]) in ``text`` are honoured — bold/italic/code
    become real Word runs and math is transliterated to Unicode — instead of
    leaking as literal ``$``/``**`` characters. ``bold``/``italic`` set the base
    style each segment is layered on top of. Returns the last run created (only
    used to keep call sites happy; callers ignore it)."""
    last: Run | None = None
    for seg in parse_inline(text):
        run = paragraph.add_run(seg.text)
        run.bold = bold or seg.bold
        run.italic = italic or seg.italic
        if size is not None:
            run.font.size = Pt(size)
        if color is not None:
            run.font.color.rgb = color
        _style_run(run, opts, mono=seg.code)
        last = run
    if last is None:  # empty text → still emit an (empty) run so layout is stable
        last = paragraph.add_run("")
        _style_run(last, opts)
    return last


_ACCENT = RGBColor(0x1F, 0x4E, 0x79)
_MUTED = RGBColor(0x55, 0x55, 0x55)


class _DocxBase(Renderer):
    media_type = _MEDIA
    extension = "docx"

    def _new_doc(self) -> Document:
        doc = Document()
        # Make the default style Devanagari-aware too, so any stray run inherits it.
        normal = doc.styles["Normal"]
        normal.font.name = self.options.body_font
        rpr = normal.element.get_or_add_rPr()
        rfonts = rpr.get_or_add_rFonts()
        rfonts.set(qn("w:ascii"), self.options.body_font)
        rfonts.set(qn("w:hAnsi"), self.options.body_font)
        rfonts.set(qn("w:cs"), self.options.devanagari_font)
        cp = doc.core_properties
        cp.author = self.options.author
        return doc

    def _heading(self, doc: Document, text: str, level: int = 1) -> None:
        p = doc.add_paragraph()
        _run(p, text, self.options, bold=True, size=16 if level == 1 else 13, color=_ACCENT)

    def _bullets(self, doc: Document, items: list[str]) -> None:
        for it in items:
            p = doc.add_paragraph(style="List Bullet")
            _run(p, it, self.options)

    def _sources(self, doc: Document, sources: list[str]) -> None:
        """The mandatory Sources section every document carries."""
        self._heading(doc, "Sources")
        self._bullets(doc, ensure_sources(sources))

    def _save(self, doc: Document, ldd: LessonDesignDocument) -> RenderedArtifact:
        buf = io.BytesIO()
        doc.save(buf)
        return self._artifact(ldd, buf.getvalue())


@register_renderer(ArtifactKind.lesson_plan, "docx")
class DocxLessonPlan(_DocxBase):
    def render(self, ldd: LessonDesignDocument) -> RenderedArtifact:
        doc = self._new_doc()
        opts = self.options

        title = doc.add_paragraph()
        _run(title, ldd.topic, opts, bold=True, size=20, color=_ACCENT)

        r = ldd.curriculum_ref
        meta = doc.add_paragraph()
        _run(meta, f"{r.board} · Grade {r.grade} · {r.subject}", opts, color=_MUTED)
        meta2 = doc.add_paragraph()
        _run(meta2,
             f"Duration: {ldd.duration_min} min   |   Framework: {ldd.framework}"
             f"   |   Language: {ldd.language}", opts, color=_MUTED)

        t = timing_summary(ldd)
        flag = "balanced" if t["balanced"] else (
            f"{'over' if t['delta_min'] > 0 else 'under'} by {abs(t['delta_min'])} min")
        tp = doc.add_paragraph()
        _run(tp, f"Timing: planned {t['planned_min']} min / target {t['target_min']} min "
                 f"({flag})", opts, italic=True, color=_MUTED)

        self._heading(doc, "Learning Objectives")
        doc.add_paragraph().add_run("By the end of the lesson, students will be able to:")
        for o in ldd.objectives:
            p = doc.add_paragraph(style="List Number")
            _run(p, o.statement, opts)
            _run(p, f"  (Bloom: {o.bloom.value})", opts, italic=True, color=_MUTED)

        if ldd.prior_knowledge:
            self._heading(doc, "Previous Knowledge")
            self._bullets(doc, ldd.prior_knowledge)

        self._heading(doc, "Engagement Hook")
        hp = doc.add_paragraph()
        _run(hp, ldd.engagement_hook.prompt, opts, italic=True)
        _run(hp, f"  — {ldd.engagement_hook.kind}", opts, color=_MUTED)

        if ldd.misconceptions:
            self._heading(doc, "Common Misconceptions")
            for m in ldd.misconceptions:
                p = doc.add_paragraph(style="List Bullet")
                _run(p, "Misconception: ", opts, bold=True)
                _run(p, m.statement, opts)
                _run(p, "  Correction: ", opts, bold=True)
                _run(p, m.correction, opts)

        if ldd.local_context:
            self._heading(doc, "Local Context")
            self._bullets(doc, ldd.local_context)

        if ldd.materials:
            self._heading(doc, "Teaching Materials")
            self._bullets(doc, ldd.materials)

        # ── the phase table ─────────────────────────────────────────────────
        self._heading(doc, f"{ldd.framework} Lesson Plan")
        table = doc.add_table(rows=1, cols=4)
        table.style = "Light Grid Accent 1"
        for cell, head in zip(table.rows[0].cells,
                              ("Phase", "Teacher Activities", "Student Activities", "Time"),
                              strict=True):
            _run(cell.paragraphs[0], head, opts, bold=True)
        for phase in ldd.phases:
            cells = table.add_row().cells
            label = phase.name_en if not phase.name_ne else f"{phase.name_en}\n({phase.name_ne})"
            _run(cells[0].paragraphs[0], label, opts, bold=True)
            self._cell_bullets(cells[1], phase.teacher_activities, opts)
            self._cell_bullets(cells[2], phase.student_activities, opts)
            _run(cells[3].paragraphs[0], f"{phase.minutes} min", opts)

        diff = ldd.differentiation
        if diff.struggling or diff.on_level or diff.advanced:
            self._heading(doc, "Differentiation")
            for label, items in (("Struggling", diff.struggling),
                                 ("On level", diff.on_level),
                                 ("Advanced", diff.advanced)):
                if items:
                    p = doc.add_paragraph(style="List Bullet")
                    _run(p, f"{label}: ", opts, bold=True)
                    _run(p, "; ".join(items), opts)

        if ldd.formative_checks:
            self._heading(doc, "Evaluation Questions")
            for q in ldd.formative_checks:
                p = doc.add_paragraph(style="List Number")
                _run(p, q.prompt, opts)

        if ldd.homework:
            self._heading(doc, "Homework")
            self._bullets(doc, ldd.homework.instructions)

        self._sources(doc, ldd.quality.grounding_sources)

        return self._save(doc, ldd)

    def _cell_bullets(self, cell, items: list[str], opts: ExportOptions) -> None:
        first = True
        for it in items:
            p = cell.paragraphs[0] if first else cell.add_paragraph()
            _run(p, f"• {it}", opts)
            first = False


class _QuestionDoc(_DocxBase):
    """Shared question + answer-key layout for worksheet and quiz."""

    heading_prefix: str

    def _questions(self, doc: Document, questions: list[Question]) -> None:
        opts = self.options
        for i, q in enumerate(questions, 1):
            p = doc.add_paragraph()
            _run(p, f"{i}. ", opts, bold=True)
            _run(p, q.prompt, opts)
            if q.type is QuestionType.mcq and q.options:
                for j, opt in enumerate(q.options):
                    op = doc.add_paragraph()
                    _run(op, f"    {chr(ord('A') + j)}.  {opt}", opts)
            elif q.type is QuestionType.true_false:
                op = doc.add_paragraph()
                _run(op, "    ( ) True     ( ) False", opts)
            else:
                op = doc.add_paragraph()
                _run(op, "    Answer: ______________________________", opts, color=_MUTED)

    def _answer_key(self, doc: Document, questions: list[Question]) -> None:
        doc.add_page_break()
        self._heading(doc, "Answer Key")
        for i, q in enumerate(questions, 1):
            p = doc.add_paragraph()
            _run(p, f"{i}. ", self.options, bold=True)
            _run(p, q.answer, self.options)


@register_renderer(ArtifactKind.worksheet, "docx")
class DocxWorksheet(_QuestionDoc):
    def render(self, source: object) -> RenderedArtifact:
        ws = Worksheet.coerce(source)
        doc = self._new_doc()
        opts = self.options
        title = doc.add_paragraph()
        _run(title, f"Worksheet — {ws.topic}", opts, bold=True, size=18, color=_ACCENT)
        name = doc.add_paragraph()
        _run(name, "Name: ____________________      Date: ____________", opts, color=_MUTED)

        self._heading(doc, "Objectives")
        for o in ws.objectives:
            p = doc.add_paragraph(style="List Number")
            _run(p, o.statement, opts)

        if ws.tasks:
            self._heading(doc, "Tasks")
            self._bullets(doc, ws.tasks)

        self._heading(doc, "Practice Questions")
        self._questions(doc, ws.questions)
        self._answer_key(doc, ws.questions)
        self._sources(doc, ws.grounding_sources)
        return self._save(doc, ws)


@register_renderer(ArtifactKind.quiz, "docx")
class DocxQuiz(_QuestionDoc):
    def render(self, source: object) -> RenderedArtifact:
        quiz = Quiz.coerce(source)
        doc = self._new_doc()
        opts = self.options
        title = doc.add_paragraph()
        _run(title, f"Quiz — {quiz.topic}", opts, bold=True, size=18, color=_ACCENT)
        info = doc.add_paragraph()
        info.alignment = WD_ALIGN_PARAGRAPH.LEFT
        _run(info, f"Name: ____________________      Score: _____ / "
                   f"{len(quiz.questions)}", opts, color=_MUTED)

        self._questions(doc, quiz.questions)
        self._answer_key(doc, quiz.questions)
        self._sources(doc, quiz.grounding_sources)
        return self._save(doc, quiz)
