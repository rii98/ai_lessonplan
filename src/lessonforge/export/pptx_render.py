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
from .base import ArtifactKind, ExportOptions, RenderedArtifact, Renderer
from .registry import register_renderer

_MEDIA = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


def _style_run(run, opts: ExportOptions) -> None:
    run.font.name = opts.body_font
    rpr = run._r.get_or_add_rPr()
    # python-pptx sets latin; add the complex-script slot for Devanagari.
    latin = rpr.find(qn("a:latin"))
    if latin is None:
        latin = rpr.makeelement(qn("a:latin"), {})
        rpr.append(latin)
    latin.set("typeface", opts.body_font)
    cs = rpr.find(qn("a:cs"))
    if cs is None:
        cs = rpr.makeelement(qn("a:cs"), {})
        rpr.append(cs)
    cs.set("typeface", opts.devanagari_font)


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
            run = para.add_run()
            run.text = line
            run.font.size = Pt(22)
            _style_run(run, self.options)

    def _textbox(self, slide, text: str, *, top: float, size: int,
                 bold: bool = False, muted: bool = False) -> None:
        box = slide.shapes.add_textbox(Inches(0.9), Inches(top), Inches(11.5), Inches(1.4))
        tf = box.text_frame
        tf.word_wrap = True
        para = tf.paragraphs[0]
        run = para.add_run()
        run.text = text
        run.font.size = Pt(size)
        run.font.bold = bold
        if muted:
            from pptx.dml.color import RGBColor
            run.font.color.rgb = RGBColor(0x55, 0x55, 0x55)
        _style_run(run, self.options)
