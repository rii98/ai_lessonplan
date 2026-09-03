"""ExportService — config-driven compilation of an LDD into teaching artifacts.

The service owns *which format* each artifact defaults to (from the ``export``
config block) and the shared :class:`ExportOptions` (fonts). It never names a
concrete renderer — it asks the registry — so swapping ``slides: pptx`` for a
future ``slides: pdf`` is a one-line config change.

:meth:`bundle` produces the one-click ``.zip`` of every configured artifact.
"""

from __future__ import annotations

import io
import zipfile
from datetime import UTC, datetime

from ..config import ExportConfig
from ..domain.ldd import LessonDesignDocument
from .base import ArtifactKind, ExportOptions, RenderedArtifact, slugify
from .registry import build_renderer, formats_for

_ZIP_MEDIA = "application/zip"
# Fixed timestamp → deterministic zip bytes for a fixed set of member bytes.
_ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)


class ExportService:
    def __init__(self, config: ExportConfig | None = None) -> None:
        self.config = config or ExportConfig()
        self.options = ExportOptions(
            body_font=self.config.body_font,
            devanagari_font=self.config.devanagari_font,
            author=self.config.author,
        )

    def default_format(self, kind: ArtifactKind) -> str:
        return getattr(self.config, kind.value)

    def render(
        self, kind: ArtifactKind, ldd: LessonDesignDocument, *, fmt: str | None = None
    ) -> RenderedArtifact:
        """Render a single artifact from a full lesson. ``fmt`` overrides the
        configured default. The targeted renderers project the LDD into their IR
        themselves, so this serves lesson_plan/slides/worksheet/quiz uniformly."""
        chosen = fmt or self.default_format(kind)
        renderer = build_renderer(kind, chosen, self.options)
        return renderer.render(ldd)

    def render_artifact(
        self, kind: ArtifactKind, ir: object, *, fmt: str | None = None
    ) -> RenderedArtifact:
        """Render a *standalone* artifact IR (a ``Quiz``/``Worksheet``/``Slides``
        produced by targeted generation) — no lesson required. Same renderer as
        the lesson-export path; the renderer accepts either shape."""
        chosen = fmt or self.default_format(kind)
        renderer = build_renderer(kind, chosen, self.options)
        return renderer.render(ir)

    def bundle(
        self, ldd: LessonDesignDocument, *, kinds: list[ArtifactKind] | None = None
    ) -> RenderedArtifact:
        """Zip every configured artifact into a single download."""
        kinds = kinds or self.config.bundle_kinds()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for kind in kinds:
                art = self.render(kind, ldd)
                info = zipfile.ZipInfo(art.filename, date_time=_ZIP_EPOCH)
                info.compress_type = zipfile.ZIP_DEFLATED
                zf.writestr(info, art.content)
        return RenderedArtifact(
            kind=ArtifactKind.lesson_plan,  # representative; it's a bundle
            fmt="zip",
            filename=f"{slugify(ldd.topic)}_bundle.zip",
            media_type=_ZIP_MEDIA,
            content=buf.getvalue(),
        )

    def manifest(self) -> dict[str, object]:
        """What the service will produce, for a discovery endpoint."""
        return {
            "artifacts": {
                k.value: {
                    "default_format": self.default_format(k),
                    "available_formats": formats_for(k),
                }
                for k in ArtifactKind
            },
            "bundle": [k.value for k in self.config.bundle_kinds()],
            "fonts": {
                "body": self.options.body_font,
                "devanagari": self.options.devanagari_font,
            },
            "generated_at": datetime.now(UTC).isoformat(),
        }
