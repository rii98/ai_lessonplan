"""Abstract interface (port) for every export renderer.

A :class:`Renderer` turns a validated :class:`LessonDesignDocument` into a
downloadable :class:`RenderedArtifact` (filename + media type + bytes). Consumers
(the :class:`~lessonforge.export.service.ExportService`, the API) depend ONLY on
this ABC. Concrete renderers live under ``export/`` and self-register with the
registry, keyed by ``(kind, fmt)`` — so adding a new artifact or a new output
format is a new class plus a one-line ``@register_renderer`` decorator, never a
change to the service or the API.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import ClassVar

from ..domain.ldd import LessonDesignDocument


class ArtifactKind(str, Enum):
    """The teaching artifacts compiled from one LDD."""

    lesson_plan = "lesson_plan"  # the 5E plan (reproduces lp1.md layout)
    slides = "slides"            # hook-first deck for the classroom
    worksheet = "worksheet"      # student tasks + answer key
    quiz = "quiz"                # MCQ / TF / short-answer + answer key


@dataclass(slots=True)
class RenderedArtifact:
    """A single downloadable file."""

    kind: ArtifactKind
    fmt: str            # e.g. "docx", "pptx", "md"
    filename: str       # suggested download name, incl. extension
    media_type: str     # HTTP Content-Type
    content: bytes


@dataclass(slots=True)
class ExportOptions:
    """Cross-cutting rendering options, sourced from ``export`` config.

    ``devanagari_font`` is stored as a complex-script font hint in DOCX/PPTX so
    Nepali (Devanagari) phase labels and terms render correctly in Word/
    PowerPoint even when the Latin body font cannot shape the script.
    """

    body_font: str = "Calibri"
    devanagari_font: str = "Noto Sans Devanagari"
    author: str = "LessonForge"


class Renderer(ABC):
    """Compiles one artifact kind into one output format."""

    kind: ClassVar[ArtifactKind]
    fmt: ClassVar[str]
    media_type: ClassVar[str]
    extension: ClassVar[str]

    def __init__(self, options: ExportOptions | None = None) -> None:
        self.options = options or ExportOptions()

    @abstractmethod
    def render(self, ldd: LessonDesignDocument) -> RenderedArtifact:
        """Produce the artifact bytes for ``ldd``."""

    # ── shared helpers ──────────────────────────────────────────────────────
    def _filename(self, ldd: LessonDesignDocument) -> str:
        return f"{slugify(ldd.topic)}_{self.kind.value}.{self.extension}"

    def _artifact(self, ldd: LessonDesignDocument, content: bytes) -> RenderedArtifact:
        return RenderedArtifact(
            kind=self.kind,
            fmt=self.fmt,
            filename=self._filename(ldd),
            media_type=self.media_type,
            content=content,
        )


def slugify(text: str, *, max_len: int = 60) -> str:
    """Filesystem-safe ASCII slug. Non-ASCII (e.g. Devanagari) is dropped so the
    download name is portable; the artifact *content* keeps full Unicode."""
    out: list[str] = []
    prev_dash = False
    for ch in text.strip().lower():
        if ch.isascii() and (ch.isalnum()):
            out.append(ch)
            prev_dash = False
        elif not prev_dash:
            out.append("-")
            prev_dash = True
    slug = "".join(out).strip("-")[:max_len].strip("-")
    return slug or "lesson"


def timing_summary(ldd: LessonDesignDocument) -> dict[str, int | bool]:
    """Surface the timing plan (M3): per-phase minutes vs the target duration.

    Renderers use this to print a timing line and flag over/under-run so a
    30/45/60-minute lesson's pacing is visible in every export.
    """
    planned = sum(p.minutes for p in ldd.phases)
    return {
        "target_min": ldd.duration_min,
        "planned_min": planned,
        "delta_min": planned - ldd.duration_min,
        "balanced": planned == ldd.duration_min,
    }
