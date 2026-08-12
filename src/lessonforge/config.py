"""Configuration: the single source of truth for which components are wired.

Load order (later wins):
    1. config/config.yaml  (with ${VAR:-default} interpolation)
    2. process environment  (LF__SECTION__KEY=value, double-underscore nesting)

Every component config carries a `provider` field. The registry (see
``providers.registry``) maps that string to a concrete adapter class, so
swapping an implementation is a one-line edit here — never a code change.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

if TYPE_CHECKING:
    from .export.base import ArtifactKind
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

_ENV_PATTERN = re.compile(r"\$\{(?P<name>[A-Z0-9_]+)(?::-(?P<default>[^}]*))?\}")


def _interpolate(value: Any) -> Any:
    """Expand ${VAR} and ${VAR:-default} inside strings, recursively."""
    if isinstance(value, str):
        def repl(m: re.Match[str]) -> str:
            return os.environ.get(m.group("name"), m.group("default") or "")

        return _ENV_PATTERN.sub(repl, value)
    if isinstance(value, dict):
        return {k: _interpolate(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate(v) for v in value]
    return value


# ── component config blocks ──────────────────────────────────────────────────
# Each block is deliberately permissive (extra="allow") so a new provider can
# introduce its own fields without touching this file.


class _ProviderBlock(BaseModel):
    model_config = {"extra": "allow"}
    provider: str


class AppConfig(BaseModel):
    model_config = {"extra": "allow"}
    name: str = "LessonForge"
    env: str = "local"


class LLMConfig(_ProviderBlock):
    model: str
    base_url: str = "http://localhost:11434"
    temperature: float = 0.7
    timeout_s: int = 180
    max_retries: int = 2


class EmbeddingConfig(_ProviderBlock):
    model: str
    dim: int | None = None  # auto-detected when None


class RerankerConfig(_ProviderBlock):
    model: str | None = None
    endpoint: str | None = None  # for the http provider


class VectorStoreConfig(_ProviderBlock):
    url: str = "http://localhost:6333"
    api_key: str | None = None
    collection_prefix: str = "lf"


class IntakeConfig(BaseModel):
    """The intake stage: raw request (possibly a pasted plan) → NormalizedBrief.

    ``llm`` uses the fast model to extract fields from a pasted plan and falls
    back to the heuristic parser on any failure; ``heuristic`` is deterministic
    (regex only, no model) — the default seam for tests and offline use.
    """

    provider: str = "llm"  # ← swap: llm | heuristic


class CritiqueConfig(BaseModel):
    """The critique→revise loop. ``provider`` picks the critic; the reviser loops
    until the weighted overall clears ``threshold`` or ``max_iterations`` is hit.

    ``structural`` scores deterministically (no LLM, no cost) — the default, so
    generation stays cheap and tests stay deterministic. ``llm`` / ``composite``
    add an LLM judge; ``noop`` disables scoring (always passes)."""

    provider: str = "structural"  # ← swap: structural | llm | composite | noop
    threshold: float = 0.7
    max_iterations: int = 1
    weights: dict[str, float] = Field(default_factory=dict)  # per-dimension; empty = equal


class RefineConfig(BaseModel):
    """The human-in-the-loop refine step (teacher reprompts one LDD section).

    ``max_cascade`` caps how many coupled neighbour sections the reviser may
    auto-repair when a scoped edit breaks a structural guardrail (e.g. a new
    objective needs a matching phase and formative check → 2). Set to 0 to
    disable cascade entirely and always reject-with-reason instead."""

    max_cascade: int = 2


class ProfileConfig(BaseModel):
    """Where teacher preferences (:class:`TeacherProfile`) are stored.

    ``memory`` is dependency-free but non-persistent (default); ``file`` writes
    one JSON per teacher under ``path`` so preferences survive restarts. The
    multi-tenant DB store is a future provider — a config swap, not a rewrite."""

    provider: str = "memory"  # ← swap: memory | file
    path: str | None = None  # directory for the file provider


class RetrievalConfig(BaseModel):
    top_k: int = 20
    rerank_top_n: int = 5


class GroundingConfig(BaseModel):
    """Which collections enrichment retrieves from, how much each contributes,
    and which brief fields become metadata filters. All swappable from config."""

    collections: list[str] = Field(
        default_factory=lambda: ["curriculum", "pedagogical", "exemplar", "local_context"]
    )
    per_collection_top_n: int = 3
    filter_fields: list[str] = Field(default_factory=lambda: ["grade", "subject"])


class ExportConfig(BaseModel):
    """Which output format each teaching artifact defaults to, plus fonts.

    Swap ``slides: pptx`` for a future ``slides: pdf`` in one line; the registry
    resolves ``(kind, format)`` to a renderer at request time. ``devanagari_font``
    is embedded as the complex-script font hint so Nepali text renders in Word/
    PowerPoint. ``bundle`` lists which artifacts the one-click zip contains.
    """

    lesson_plan: str = "docx"
    slides: str = "pptx"
    worksheet: str = "docx"
    quiz: str = "docx"
    bundle: list[str] = Field(default_factory=lambda: ["lesson_plan", "slides", "worksheet", "quiz"])
    body_font: str = "Calibri"
    devanagari_font: str = "Noto Sans Devanagari"
    author: str = "LessonForge"

    def bundle_kinds(self) -> list[ArtifactKind]:
        # imported lazily to avoid a config→export import cycle
        from .export.base import ArtifactKind

        return [ArtifactKind(k) for k in self.bundle]


class Settings(BaseSettings):
    """Root settings object. Built by :func:`load_settings`."""

    model_config = SettingsConfigDict(
        env_prefix="LF__",
        env_nested_delimiter="__",
        extra="ignore",
    )

    app: AppConfig = Field(default_factory=AppConfig)
    llm: LLMConfig
    # Per-stage model selection (SRD §07): a cheap fast model for intake/extraction,
    # the strong reasoning model (``llm``) for enrichment + critique. Optional —
    # when absent, the fast stages reuse ``llm``. Same block shape as ``llm``.
    llm_fast: LLMConfig | None = None
    embedding: EmbeddingConfig
    reranker: RerankerConfig
    vector_store: VectorStoreConfig
    retrieval: RetrievalConfig = Field(default_factory=RetrievalConfig)
    grounding: GroundingConfig = Field(default_factory=GroundingConfig)
    intake: IntakeConfig = Field(default_factory=IntakeConfig)
    critique: CritiqueConfig = Field(default_factory=CritiqueConfig)
    refine: RefineConfig = Field(default_factory=RefineConfig)
    profile: ProfileConfig = Field(default_factory=ProfileConfig)
    export: ExportConfig = Field(default_factory=ExportConfig)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # env vars (LF__...) take priority over YAML values passed as init kwargs.
        return (env_settings, init_settings, dotenv_settings, file_secret_settings)


def _default_config_path() -> Path:
    """Locate config.yaml robustly across dev (src layout) and installed/docker.

    Order: explicit LF_CONFIG env, then repo-relative to this file, then the
    working directory (WORKDIR /app in the container). First existing wins;
    otherwise the last candidate is returned so the error names a real path.
    """
    override = os.environ.get("LF_CONFIG")
    if override:
        return Path(override)
    candidates = [
        Path(__file__).resolve().parents[2] / "config" / "config.yaml",  # repo/src layout
        Path.cwd() / "config" / "config.yaml",                            # e.g. /app/config
    ]
    for c in candidates:
        if c.exists():
            return c
    return candidates[-1]


def load_settings(path: str | Path | None = None) -> Settings:
    """Load YAML config, interpolate env vars, then let env vars override."""
    cfg_path = Path(path) if path else _default_config_path()
    raw: dict[str, Any] = {}
    if cfg_path.exists():
        raw = yaml.safe_load(cfg_path.read_text()) or {}
    raw = _interpolate(raw)
    # BaseSettings merges env (LF__...) on top of the values passed in.
    return Settings(**raw)
