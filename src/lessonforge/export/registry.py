"""Renderer registry — maps ``(ArtifactKind, format)`` to a renderer class.

Mirrors ``providers.registry``: renderers self-register via a decorator, and a
lookup for an unknown pair fails loudly with the list of what IS available. This
is what keeps export loosely coupled — the service and API never name a concrete
renderer class.
"""

from __future__ import annotations

from .base import ArtifactKind, ExportOptions, Renderer

RENDERER_REGISTRY: dict[tuple[ArtifactKind, str], type[Renderer]] = {}


def register_renderer(kind: ArtifactKind, fmt: str):
    def deco(cls: type[Renderer]) -> type[Renderer]:
        key = (kind, fmt)
        if key in RENDERER_REGISTRY:
            raise ValueError(
                f"renderer {key} already registered as {RENDERER_REGISTRY[key]!r}"
            )
        # keep the class's own metadata honest with its registration key
        cls.kind = kind
        cls.fmt = fmt
        RENDERER_REGISTRY[key] = cls
        return cls

    return deco


def build_renderer(
    kind: ArtifactKind, fmt: str, options: ExportOptions | None = None
) -> Renderer:
    try:
        cls = RENDERER_REGISTRY[(kind, fmt)]
    except KeyError:
        formats = sorted({f for k, f in RENDERER_REGISTRY if k == kind})
        available = ", ".join(formats) or "<none registered>"
        raise ValueError(
            f"No renderer for {kind.value!r} in format {fmt!r}. "
            f"Available formats for {kind.value!r}: {available}."
        ) from None
    return cls(options)


def formats_for(kind: ArtifactKind) -> list[str]:
    return sorted({f for k, f in RENDERER_REGISTRY if k == kind})
