"""PPTX renderer (python-pptx) — a hook-first classroom deck.

Slide order deliberately leads with the engagement hook (not the title/definition)
so the lesson opens on curiosity, matching the pedagogy the LDD enforces:

    Title → **Hook** → Objectives → one slide per phase → Check for Understanding

Devanagari phase labels are shaped by setting the complex-script font on every
run, same technique as the DOCX renderer.
"""

from __future__ import annotations

import io

from pptx import Presentation
from pptx.oxml.ns import qn
from pptx.util import Inches, Pt

from ..domain.artifacts import Quiz, Slides, Worksheet
from ..domain.assessment import (
    AssessmentSpec,
    QuestionView,
    build_sections,
    flat_views,
    full_marks,
)
from ..domain.ldd import Question, QuestionType
from ..rag.grounding import ensure_sources
from .base import ArtifactKind, ExportOptions, RenderedArtifact, Renderer
from .registry import register_renderer
from .richtext import parse_inline

_MEDIA = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
_MONO = "Consolas"


def _style_run(run, opts: ExportOptions, *, mono: bool = False) -> None:
    face = _MONO if mono else opts.body_font
    run.font.name = face
    rpr = run._r.get_or_add_rPr()
    # python-pptx sets latin; add the complex-script slot for Devanagari.
    latin = rpr.find(qn("a:latin"))
    if latin is None:
        latin = rpr.makeelement(qn("a:latin"), {})
        rpr.append(latin)
    latin.set("typeface", face)
    cs = rpr.find(qn("a:cs"))
    if cs is None:
        cs = rpr.makeelement(qn("a:cs"), {})
        rpr.append(cs)
    cs.set("typeface", opts.devanagari_font)


def _add_rich(para, text: str, opts: ExportOptions, *, size: int,
              bold: bool = False, color=None) -> None:
    """Add ``text`` to a paragraph as styled runs, honouring inline Markdown
    (**bold**, *italic*, `code`), fenced code blocks, inline HTML (<b>, <code>,
    <br>, entities) and transliterating LaTeX math to Unicode — the PPTX
    counterpart to the DOCX ``_run`` helper. Newlines become soft line breaks."""
    from pptx.util import Pt as _Pt

    def _emit(chunk: str, seg) -> None:
        run = para.add_run()
        run.text = chunk
        run.font.size = _Pt(size)
        run.font.bold = bool(bold or (seg and seg.bold))
        run.font.italic = bool(seg and seg.italic)
        if color is not None:
            run.font.color.rgb = color
        _style_run(run, opts, mono=bool(seg and seg.code))

    for seg in parse_inline(text) or [None]:
        lines = (seg.text if seg else "").split("\n")
        _emit(lines[0], seg)
        for extra in lines[1:]:            # newline → line break within the run
            para.add_line_break()
            _emit(extra, seg)


class _PptxDeck(Renderer):
    """Shared 16:9 canvas + slide builders for every deck-shaped renderer."""

    media_type = _MEDIA
    extension = "pptx"

    def _new_prs(self) -> Presentation:
        prs = Presentation()
        prs.slide_width = Inches(13.333)
        prs.slide_height = Inches(7.5)
        return prs

    def _save(self, prs: Presentation, doc: object) -> RenderedArtifact:
        buf = io.BytesIO()
        prs.save(buf)
        return self._artifact(doc, buf.getvalue())

    # ── slide builders ──────────────────────────────────────────────────────
    def _title_slide(self, prs: Presentation, title: str, subtitle: str) -> None:
        slide = prs.slides.add_slide(prs.slide_layouts[6])  # blank
        self._textbox(slide, title, top=2.4, size=40, bold=True)
        self._textbox(slide, subtitle, top=4.0, size=18, muted=True)

    def _bullet_slide(self, prs: Presentation, title: str, bullets: list[str],
                      *, subtitle: str | None = None) -> None:
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        self._textbox(slide, title, top=0.5, size=30, bold=True)
        if subtitle:
            self._textbox(slide, subtitle, top=1.25, size=16, muted=True)
        box = slide.shapes.add_textbox(Inches(0.9), Inches(1.9),
                                       Inches(11.5), Inches(5.0))
        tf = box.text_frame
        tf.word_wrap = True
        for i, line in enumerate(bullets or ["—"]):
            para = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
            para.space_after = Pt(10)
            _add_rich(para, line, self.options, size=22)

    def _textbox(self, slide, text: str, *, top: float, size: int,
                 bold: bool = False, muted: bool = False) -> None:
        box = slide.shapes.add_textbox(Inches(0.9), Inches(top), Inches(11.5), Inches(1.4))
        tf = box.text_frame
        tf.word_wrap = True
        para = tf.paragraphs[0]
        color = None
        if muted:
            from pptx.dml.color import RGBColor
            color = RGBColor(0x55, 0x55, 0x55)
        _add_rich(para, text, self.options, size=size, bold=bold, color=color)


@register_renderer(ArtifactKind.slides, "pptx")
class PptxSlides(_PptxDeck):
    def render(self, source: object) -> RenderedArtifact:
        deck = Slides.coerce(source)
        prs = self._new_prs()

        self._title_slide(prs, deck.topic, deck.subtitle)
        for slide in deck.slides:
            self._bullet_slide(prs, slide.heading, slide.bullets, subtitle=slide.subtitle)
        # mandatory Sources slide, last
        self._bullet_slide(prs, "Sources", ensure_sources(deck.grounding_sources))
        return self._save(prs, deck)


# ── quiz / worksheet decks ───────────────────────────────────────────────────
_KEY_CHARS_PER_SLIDE = 1100   # answer-key slides are filled by text budget, not item count
_KEY_ITEM_CHARS = 220         # a long model answer is clipped on the slide (full text is
                              # in the question slide's speaker notes)


def _clip(text: str, n: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


class _PptxQuestionDeck(_PptxDeck):
    """A classroom-projectable question deck: title → (objectives/tasks) → for each
    section a divider, then ONE slide per question → answer-key slides → sources.

    The answer for every question is also placed in that slide's *speaker notes*, so a
    teacher presenting sees the key privately while students see only the question."""

    label: str  # "Quiz" / "Worksheet"

    def _question_slides(self, prs: Presentation, questions: list[Question],
                         spec: AssessmentSpec | None) -> list[QuestionView]:
        """Section dividers (with a blueprint) + one slide per question, in order."""
        sections = build_sections(questions, spec)
        views: list[QuestionView] = []
        if sections is None:
            for v in flat_views(questions):
                self._question_slide(prs, v, "")
                views.append(v)
            return views
        for sec in sections:
            self._bullet_slide(prs, f"Section {sec.letter} — {sec.title}", [sec.instruction],
                               subtitle=sec.marks_note or None)
            for v in sec.items:
                self._question_slide(prs, v, f"Section {sec.letter}")
                views.append(v)
        return views

    def _question_slide(self, prs: Presentation, v: QuestionView, section: str) -> None:
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        self._textbox(slide, f"Question {v.number}", top=0.4, size=28, bold=True)
        meta = [m for m in (section, f"{v.marks} mark{'s' if v.marks != 1 else ''}"
                            if v.marks else "") if m]
        if meta:
            self._textbox(slide, "  ·  ".join(meta), top=1.15, size=16, muted=True)

        long_prompt = len(v.prompt) > 220
        top = 1.8
        box = slide.shapes.add_textbox(Inches(0.9), Inches(top), Inches(11.5), Inches(1.6))
        tf = box.text_frame
        tf.word_wrap = True
        _add_rich(tf.paragraphs[0], v.prompt, self.options, size=22 if long_prompt else 28)
        top += 1.9 if long_prompt else 1.5

        if v.type is QuestionType.matching:
            self._match_table(slide, v, top)
        elif v.choices or v.true_false or v.response_lines:
            lines = v.choices or (["( )  True", "( )  False"] if v.true_false else
                                  ["Write your answer…"])
            body = slide.shapes.add_textbox(Inches(1.2), Inches(top), Inches(11), Inches(3.6))
            btf = body.text_frame
            btf.word_wrap = True
            for i, line in enumerate(lines):
                para = btf.paragraphs[0] if i == 0 else btf.add_paragraph()
                para.space_after = Pt(8)
                _add_rich(para, line, self.options, size=24)
        notes = [f"ANSWER: {v.answer}"]
        notes += [f"• {kp}" for kp in v.key_points]
        if v.difficulty:
            notes.append(f"(difficulty: {v.difficulty})")
        slide.notes_slide.notes_text_frame.text = "\n".join(notes)

    def _match_table(self, slide, v: QuestionView, top: float) -> None:
        rows = len(v.left)
        shape = slide.shapes.add_table(rows + 1, 2, Inches(0.9), Inches(top),
                                       Inches(11.5), Inches(0.5 * (rows + 1)))
        table = shape.table
        for col, head in enumerate(("Column A", "Column B")):
            self._cell(table.cell(0, col), head, bold=True)
        for r, (a, b) in enumerate(zip(v.left, v.right, strict=True), 1):
            self._cell(table.cell(r, 0), a)
            self._cell(table.cell(r, 1), b)

    def _cell(self, cell, text: str, *, bold: bool = False) -> None:
        cell.text_frame.word_wrap = True
        _add_rich(cell.text_frame.paragraphs[0], text, self.options, size=20, bold=bold)

    def _key_slides(self, prs: Presentation, views: list[QuestionView]) -> None:
        pages: list[list[str]] = [[]]
        used = 0
        for v in views:
            line = f"{v.number}. {_clip(v.answer, _KEY_ITEM_CHARS)}"
            if pages[-1] and used + len(line) > _KEY_CHARS_PER_SLIDE:
                pages.append([])
                used = 0
            pages[-1].append(line)
            used += len(line)
        for i, lines in enumerate(pages, 1):
            suffix = f" ({i}/{len(pages)})" if len(pages) > 1 else ""
            self._bullet_slide(prs, f"Answer Key{suffix}", lines)


@register_renderer(ArtifactKind.quiz, "pptx")
class PptxQuiz(_PptxQuestionDeck):
    label = "Quiz"

    def render(self, source: object) -> RenderedArtifact:
        quiz = Quiz.coerce(source)
        prs = self._new_prs()
        r = quiz.curriculum_ref
        bits = [f"{r.board} · Grade {r.grade} · {r.subject}", f"{len(quiz.questions)} questions"]
        if marks := full_marks(build_sections(quiz.questions, quiz.spec)):
            bits.append(f"Full marks {marks}")
        self._title_slide(prs, f"Quiz — {quiz.topic}", "  ·  ".join(bits))
        views = self._question_slides(prs, quiz.questions, quiz.spec)
        self._key_slides(prs, views)
        self._bullet_slide(prs, "Sources", ensure_sources(quiz.grounding_sources))
        return self._save(prs, quiz)


@register_renderer(ArtifactKind.worksheet, "pptx")
class PptxWorksheet(_PptxQuestionDeck):
    label = "Worksheet"

    def render(self, source: object) -> RenderedArtifact:
        ws = Worksheet.coerce(source)
        prs = self._new_prs()
        # objectives + tasks first (a worksheet leads with the work, then practice)
        self._title_slide(prs, f"Worksheet — {ws.topic}",
                          f"{ws.curriculum_ref.board} · Grade {ws.curriculum_ref.grade} · "
                          f"{ws.curriculum_ref.subject}")
        self._bullet_slide(prs, "Objectives", [o.statement for o in ws.objectives])
        if ws.tasks:
            self._bullet_slide(prs, "Tasks", [f"{i}. {t}" for i, t in enumerate(ws.tasks, 1)])
        views = self._question_slides(prs, ws.questions, ws.spec)
        self._key_slides(prs, views)
        self._bullet_slide(prs, "Sources", ensure_sources(ws.grounding_sources))
        return self._save(prs, ws)
