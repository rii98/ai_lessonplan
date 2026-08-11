"""Export subsystem: LDD → downloadable teaching artifacts.

Importing this package registers every renderer with the registry (same pattern
as ``providers``: import triggers ``@register_renderer`` side effects). Consumers
should import from here so registration is guaranteed before ``build_renderer``
is called.
"""

from __future__ import annotations

from . import docx_render, markdown, pptx_render  # noqa: F401  (registration side effects)
from .base import ArtifactKind, ExportOptions, RenderedArtifact, Renderer
from .registry import RENDERER_REGISTRY, build_renderer, formats_for
from .service import ExportService

__all__ = [
    "RENDERER_REGISTRY",
    "ArtifactKind",
    "ExportOptions",
    "ExportService",
    "RenderedArtifact",
    "Renderer",
    "build_renderer",
    "formats_for",
]
