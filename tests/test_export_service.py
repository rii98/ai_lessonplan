"""ExportService — config picks the default format per artifact, callers can
override, and the bundle is a deterministic zip of every configured artifact."""

from __future__ import annotations

import io
import zipfile

from lessonforge.config import ExportConfig
from lessonforge.export import ArtifactKind
from lessonforge.export.service import ExportService


def test_default_format_comes_from_config(export_ldd):
    svc = ExportService(ExportConfig(lesson_plan="md"))
    art = svc.render(ArtifactKind.lesson_plan, export_ldd)
    assert art.fmt == "md"
    assert art.media_type.startswith("text/markdown")


def test_format_override_beats_config(export_ldd):
    svc = ExportService(ExportConfig(lesson_plan="docx"))
    art = svc.render(ArtifactKind.lesson_plan, export_ldd, fmt="md")
    assert art.fmt == "md"


def test_fonts_flow_from_config_into_options(export_ldd):
    svc = ExportService(ExportConfig(devanagari_font="Mangal"))
    assert svc.options.devanagari_font == "Mangal"
    art = svc.render(ArtifactKind.lesson_plan, export_ldd, fmt="docx")
    xml = zipfile.ZipFile(io.BytesIO(art.content)).read("word/document.xml").decode()
    assert 'w:cs="Mangal"' in xml


def test_bundle_contains_every_configured_artifact(export_ldd):
    svc = ExportService()  # defaults: lesson_plan, slides, worksheet, quiz
    bundle = svc.bundle(export_ldd)
    assert bundle.media_type == "application/zip"
    assert bundle.filename.endswith("_bundle.zip")
    names = zipfile.ZipFile(io.BytesIO(bundle.content)).namelist()
    assert len(names) == 4
    assert any(n.endswith("_lesson_plan.docx") for n in names)
    assert any(n.endswith("_slides.pptx") for n in names)
    assert any(n.endswith("_worksheet.docx") for n in names)
    assert any(n.endswith("_quiz.docx") for n in names)


def test_bundle_respects_custom_kind_list(export_ldd):
    svc = ExportService(ExportConfig(bundle=["lesson_plan", "quiz"], lesson_plan="md", quiz="md"))
    names = zipfile.ZipFile(io.BytesIO(svc.bundle(export_ldd).content)).namelist()
    assert sorted(names) == [
        "components-of-environment-biotic-and-abiotic_lesson_plan.md",
        "components-of-environment-biotic-and-abiotic_quiz.md",
    ]


def test_bundle_zip_metadata_is_deterministic(export_ldd):
    """Zip member timestamps are pinned, so identical content → identical bytes."""
    svc = ExportService(ExportConfig(bundle=["lesson_plan"], lesson_plan="md"))
    a = svc.bundle(export_ldd).content
    b = svc.bundle(export_ldd).content
    assert a == b


def test_manifest_reports_formats_and_fonts():
    svc = ExportService()
    m = svc.manifest()
    assert m["artifacts"]["lesson_plan"]["default_format"] == "docx"
    assert "md" in m["artifacts"]["lesson_plan"]["available_formats"]
    assert m["bundle"] == ["lesson_plan", "slides", "worksheet", "quiz"]
    assert m["fonts"]["devanagari"] == "Noto Sans Devanagari"
