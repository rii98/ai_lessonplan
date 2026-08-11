"""The renderer registry is the loose-coupling seam for export: config names a
(kind, format) pair; the registry resolves it to a class. Unknown pairs must fail
loudly, never silently."""

from __future__ import annotations

import pytest

import lessonforge.export  # noqa: F401  (registers renderers)
from lessonforge.export.base import ArtifactKind, ExportOptions
from lessonforge.export.registry import build_renderer, formats_for, register_renderer


def test_every_artifact_kind_has_at_least_one_renderer():
    for kind in ArtifactKind:
        assert formats_for(kind), f"no renderer registered for {kind.value}"


def test_build_renderer_returns_the_right_kind_and_format():
    r = build_renderer(ArtifactKind.lesson_plan, "docx")
    assert r.kind is ArtifactKind.lesson_plan
    assert r.fmt == "docx"


def test_build_renderer_passes_options_through():
    opts = ExportOptions(devanagari_font="Mangal")
    r = build_renderer(ArtifactKind.slides, "pptx", opts)
    assert r.options.devanagari_font == "Mangal"


def test_unknown_format_fails_loudly_listing_available():
    with pytest.raises(ValueError, match="No renderer for 'slides' in format 'pdf'"):
        build_renderer(ArtifactKind.slides, "pdf")


def test_duplicate_registration_is_rejected():
    with pytest.raises(ValueError, match="already registered"):

        @register_renderer(ArtifactKind.lesson_plan, "docx")
        class _Dupe:  # pragma: no cover - registration raises before use
            pass
