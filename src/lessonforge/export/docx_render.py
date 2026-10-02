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
from ..domain.assessment import (
    AssessmentSpec,
    QuestionView,
    build_sections,
    flat_views,
    full_marks,
)
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

    Inline Markdown (``**bold**``, ``*italic*``, ``` `code` ```), fenced code
    blocks, inline HTML (``<b>``, ``<code>``, ``<br>``, entities) and LaTeX math
    ($…$, $$…$$, \\(..\\), \\[..\\]) in ``text`` are honoured — emphasis/code
    become real Word runs, HTML folds onto the same styles, and math is
    transliterated to Unicode — instead of leaking as literal markup. Newlines
    (from a fenced block or ``<br>``) become real line breaks. ``bold``/
    ``italic`` set the base style each segment is layered on top of. Returns the
    last run created (callers ignore it)."""
    last: Run | None = None
    for seg in parse_inline(text):
        lines = seg.text.split("\n")
        run = paragraph.add_run(lines[0])
        for extra in lines[1:]:            # newline → real Word line break
            run.add_break()
            run.add_text(extra)
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
    """Shared question + answer-key layout for worksheet and quiz.

    With a blueprint the questions print in lettered sections (heading, student
    instruction, marks); without one — a quiz projected from a lesson — they print as
    the flat numbered list they always have. Both go through the same per-question
    view, so every type (matching table, scrambled steps, ruled answer lines…) is
    laid out identically either way."""

    heading_prefix: str

    def _body(self, doc: Document, questions: list[Question],
              spec: AssessmentSpec | None) -> list[QuestionView]:
        sections = build_sections(questions, spec)
        if sections is None:
            views = flat_views(questions)
            for v in views:
                self._question(doc, v)
            return views
        views: list[QuestionView] = []
        opts = self.options
        for sec in sections:
            head = doc.add_paragraph()
            head.paragraph_format.keep_with_next = True
            head.paragraph_format.space_before = Pt(10)
            _run(head, f"Section {sec.letter} — {sec.title}", opts, bold=True, size=13,
                 color=_ACCENT)
            if sec.marks_note:
                _run(head, f"   [{sec.marks_note}]", opts, color=_MUTED)
            ins = doc.add_paragraph()
            ins.paragraph_format.keep_with_next = True
            _run(ins, sec.instruction, opts, italic=True, color=_MUTED)
            for v in sec.items:
                self._question(doc, v, show_marks=True)
                views.append(v)
        return views

    def _question(self, doc: Document, v: QuestionView, *, show_marks: bool = False) -> None:
        opts = self.options
        p = doc.add_paragraph()
        p.paragraph_format.keep_with_next = True
        _run(p, f"{v.number}. ", opts, bold=True)
        _run(p, v.prompt, opts)
        if show_marks and v.marks:
            _run(p, f"  [{v.marks}]", opts, color=_MUTED)
        if v.type is QuestionType.matching:
            self._match_table(doc, v)
        elif v.choices:
            for c in v.choices:
                op = doc.add_paragraph()
                op.paragraph_format.keep_with_next = True
                _run(op, f"    {c.replace('. ', '.  ', 1)}", opts)
            if v.type is QuestionType.ordering:
                _run(doc.add_paragraph(), "    Correct order:  ____  →  ____  →  ____  →  ____",
                     opts, color=_MUTED)
        elif v.true_false:
            _run(doc.add_paragraph(), "    ( ) True     ( ) False", opts)
        elif v.type is QuestionType.fill_blank:
            pass  # the blank is in the prompt
        else:
            _run(doc.add_paragraph(), "    Answer: ______________________________", opts,
                 color=_MUTED)
            for _ in range(max(0, v.response_lines - 1)):
                _run(doc.add_paragraph(),
                     "    ____________________________________________________________",
                     opts, color=_MUTED)

    def _match_table(self, doc: Document, v: QuestionView) -> None:
        """Column A / Column B as a borderless two-column table (a real table keeps the
        columns aligned however long the entries are, in Word and when imported)."""
        opts = self.options
        table = doc.add_table(rows=1 + len(v.left), cols=2)
        for cell, head in zip(table.rows[0].cells, ("Column A", "Column B"), strict=True):
            _run(cell.paragraphs[0], head, opts, bold=True)
        for row, (a, b) in zip(table.rows[1:], zip(v.left, v.right, strict=True), strict=True):
            _run(row.cells[0].paragraphs[0], a, opts)
            _run(row.cells[1].paragraphs[0], b, opts)
        _run(doc.add_paragraph(),
             "    Answer:  " + "   ".join(f"{i}–___" for i in range(1, len(v.left) + 1)),
             opts, color=_MUTED)

    def _answer_key(self, doc: Document, views: list[QuestionView]) -> None:
        doc.add_page_break()
        self._heading(doc, "Answer Key")
        for v in views:
            p = doc.add_paragraph()
            _run(p, f"{v.number}. ", self.options, bold=True)
            _run(p, v.answer, self.options)
            if v.difficulty:
                _run(p, f"   ({v.difficulty})", self.options, italic=True, color=_MUTED)
            for kp in v.key_points:
                kpp = doc.add_paragraph(style="List Bullet")
                _run(kpp, kp, self.options, color=_MUTED)

    def _marks_line(self, questions: list[Question], spec: AssessmentSpec | None) -> str:
        total = full_marks(build_sections(questions, spec))
        return f"      Full marks: {total}" if total else ""


@register_renderer(ArtifactKind.worksheet, "docx")
class DocxWorksheet(_QuestionDoc):
    def render(self, source: object) -> RenderedArtifact:
        ws = Worksheet.coerce(source)
        doc = self._new_doc()
        opts = self.options
        title = doc.add_paragraph()
        _run(title, f"Worksheet — {ws.topic}", opts, bold=True, size=18, color=_ACCENT)
        name = doc.add_paragraph()
        _run(name, "Name: ____________________      Date: ____________"
             + self._marks_line(ws.questions, ws.spec), opts, color=_MUTED)

        self._heading(doc, "Objectives")
        for o in ws.objectives:
            p = doc.add_paragraph(style="List Number")
            _run(p, o.statement, opts)

        if ws.tasks:
            self._heading(doc, "Tasks")
            self._bullets(doc, ws.tasks)

        self._heading(doc, "Practice Questions")
        views = self._body(doc, ws.questions, ws.spec)
        self._answer_key(doc, views)
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
        total = full_marks(build_sections(quiz.questions, quiz.spec)) or len(quiz.questions)
        _run(info, f"Name: ____________________      Score: _____ / {total}", opts, color=_MUTED)

        views = self._body(doc, quiz.questions, quiz.spec)
        self._answer_key(doc, views)
        self._sources(doc, quiz.grounding_sources)
        return self._save(doc, quiz)
