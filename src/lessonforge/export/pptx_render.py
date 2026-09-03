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

from ..domain.artifacts import Slides
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


@register_renderer(ArtifactKind.slides, "pptx")
class PptxSlides(Renderer):
    media_type = _MEDIA
    extension = "pptx"

    def render(self, source: object) -> RenderedArtifact:
        deck = Slides.coerce(source)
        prs = Presentation()
        prs.slide_width = Inches(13.333)
        prs.slide_height = Inches(7.5)

        self._title_slide(prs, deck.topic, deck.subtitle)
        for slide in deck.slides:
            self._bullet_slide(prs, slide.heading, slide.bullets, subtitle=slide.subtitle)
        # mandatory Sources slide, last
        self._bullet_slide(prs, "Sources", ensure_sources(deck.grounding_sources))

        buf = io.BytesIO()
        prs.save(buf)
        return self._artifact(deck, buf.getvalue())

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
