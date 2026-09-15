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


class SparseEmbeddingConfig(_ProviderBlock):
    """The lexical encoder for hybrid search (e.g. BM25). Optional — only built and
    used when ``retrieval.hybrid`` is on."""

    model: str = "Qdrant/bm25"


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


class GenerationConfig(BaseModel):
    """The model→domain boundary for generation. ``max_repairs`` is how many times
    the assembler may feed a validation failure back to the model to self-correct
    before degrading to a clean error (0 disables the repair loop, leaving only the
    deterministic normalization layer)."""

    max_repairs: int = 1


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
    # Hybrid = dense (semantic) ⊕ sparse (lexical/BM25), RRF-fused before rerank.
    # On by default; degrades to dense-only for stores/collections without a sparse
    # index, so it's safe before existing corpora are re-ingested.
    hybrid: bool = True


class ChunkingConfig(BaseModel):
    """How raw documents are split before embedding. ``default`` names the chunker
    for any source; ``by_format`` overrides it per loader format (so markdown books
    get the structure-aware chunker while JSONL keeps paragraph packing). ``params``
    are constructor kwargs passed to whichever chunker is built — one place to tune
    ``max_chars``/``min_chars`` without code. Chunker names resolve through the
    chunker registry (``rag.chunkers.build_chunker``)."""

    default: str = "paragraph"
    by_format: dict[str, str] = Field(default_factory=lambda: {"markdown": "markdown"})
    params: dict[str, Any] = Field(default_factory=dict)


class GroundingConfig(BaseModel):
    """Which collections enrichment retrieves from, how much each contributes,
    and which brief fields become metadata filters. All swappable from config."""

    collections: list[str] = Field(
        default_factory=lambda: [
            "reference", "curriculum", "pedagogical", "exemplar", "local_context",
        ]
    )
    per_collection_top_n: int = 3
    filter_fields: list[str] = Field(default_factory=lambda: ["grade", "subject"])
    # Coverage (unit planning): how many top vector hits are inspected to *locate*
    # the chapter(s) a unit spans before its full section list is enumerated by
    # metadata scroll. Only the anchors' (doc_id, chapter) matter — their chapters
    # are unioned — so this is recall-biased: several are read (not just the best)
    # so a chapter whose best chunk ranks a few places down is still found. Over-
    # inclusion (a stray extra chapter) is acceptable; a missing section is not.
    coverage_anchors: int = 8
    # Coverage granularity: how many heading levels *below the chapter* count as a
    # distinct topic in the coverage outline. 1 = the numbered sections (12.1, 12.2,
    # …) — the teacher's notion of "topics to cover"; deeper sub-headings and
    # callout boxes ("Do You Know", "Main features:") collapse into their parent
    # section instead of each becoming a mandatory item. A section that exists only
    # via its sub-headings is preserved (its path is truncated up to this depth), so
    # raising granularity never *loses* a topic — it only splits coarse ones finer.
    coverage_depth: int = 1
    # Default retrieval granularity for authoritative collections: narrow (the
    # reranked chunks), section (each hit expanded to its whole section), or broad
    # (expanded to its whole chapter). narrow keeps today's behaviour; a planner
    # (or a caller) can override per request. Only authoritative collections honor
    # it — flat seed corpora have no hierarchy to expand into and stay narrow.
    default_granularity: str = "narrow"
    # Per-hit budget when a section/chapter is reassembled from its chunks, so a
    # broad expansion can't overflow the enrichment prompt.
    broad_max_chars: int = 8000
    # ── quality gate (recall-biased; off by default) ──────────────────────────
    # Top-anchored garbage floor: if even the BEST reranked hit for a collection
    # scores below this, the whole result is dropped as "no usable context" and
    # generation degrades to the honest model-knowledge marker — rather than
    # feeding the prompt the least-irrelevant paragraph. It never prunes individual
    # marginal hits (a correct-but-low-scoring chunk survives as long as the top
    # hit clears the bar), so it can't silently drop a good answer. None disables
    # it; calibrate the value from the shadow-logged reranker distribution
    # (lessonforge.rag.retriever.scores), since reranker score scales vary.
    min_score: float | None = None
    # Optional LLM relevance check on the top authoritative hit (the real judge for
    # high-stakes broad generations): a correct-but-low-scoring chunk is rescued and
    # a high-scoring-but-off-topic one is caught. Uses the fast model; off by default.
    verify: bool = False
    # Collections whose material is authoritative course content: when they return
    # hits, the prompt is told to PREFER them over the model's general knowledge and
    # to cite them. When they're empty (no book ingested for this topic), grounding
    # degrades to the model's own reasoning — no special-casing needed.
    authoritative_collections: list[str] = Field(default_factory=lambda: ["reference"])
    # Collections whose records are ALSO filtered by the lesson's framework, so a
    # gradual_release build retrieves gradual_release exemplars — not 5E ones.
    # Only collections whose records carry a `framework` tag belong here
    # (curriculum/local_context are framework-agnostic).
    framework_filter_collections: list[str] = Field(default_factory=lambda: ["exemplar"])


class PlannerConfig(BaseModel):
    """The retrieval planner: decides how much context a need requires (narrow /
    section / broad) before retrieving, so a unit plan pulls a chapter while a
    quiz question pulls one chunk.

    ``heuristic`` is free and deterministic (need-kind + query stems); ``llm``
    asks the fast model to classify (degrading to the heuristic on any failure);
    ``hybrid`` uses the heuristic and only spends an LLM call when it is
    ambiguous. ``cache`` memoizes plans by (kind, need) so a multi-day build
    doesn't re-decide the same need for every day."""

    provider: str = "heuristic"  # ← swap: heuristic | llm | hybrid
    default_granularity: str = "narrow"
    cache: bool = True


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


class ChatStoreConfig(BaseModel):
    """Where the chatbot's conversations + messages are persisted.

    ``memory`` is dependency-free but non-persistent (tests/dev); ``sqlite``
    writes a single file (no service needed); ``postgres`` is the real, Docker-
    backed store. The connection string is env-driven (``url``), never hard-coded
    in YAML — a secret does not belong in config. Same pluggable-port pattern as
    :class:`ProfileConfig`."""

    provider: str = "memory"  # ← swap: memory | sqlite | postgres
    url: str | None = None    # postgres DSN, e.g. ${CHAT_DB_URL}
    path: str | None = None   # sqlite file (default: .lessonforge/chat.db)


class DocumentStoreConfig(BaseModel):
    """Where generated lessons/units and their version history are persisted.

    Same pluggable-port pattern as :class:`ChatStoreConfig`: ``memory`` is
    dependency-free but non-persistent (tests/dev); ``sqlite`` writes a single
    file (no service); ``postgres`` is the Docker-backed store (DSN via ``url``,
    env-driven). The memory layer that makes lessons revisitable, editable, and
    undoable."""

    provider: str = "memory"  # ← swap: memory | sqlite | postgres
    url: str | None = None    # postgres DSN, e.g. ${DOCS_DB_URL}
    path: str | None = None   # sqlite file (default: .lessonforge/documents.db)


class ChatRetrievalConfig(BaseModel):
    """Retrieval for the QA chatbot (distinct from lesson ``grounding``).

    ``collections`` are searched per query; ``filter_fields`` name which of a
    conversation's default tags become metadata filters in *scoped* mode.
    ``default_mode`` is the fallback when a message doesn't override it:
    ``scoped`` applies the tag filters (lenient fallback), ``broad`` ignores
    them and searches everything."""

    collections: list[str] = Field(
        default_factory=lambda: [
            "reference", "curriculum", "pedagogical", "exemplar", "local_context",
        ]
    )
    per_collection_top_n: int = 4
    top_k: int = 20
    rerank_top_n: int = 6
    filter_fields: list[str] = Field(default_factory=lambda: ["grade", "subject", "class"])
    default_mode: str = "scoped"  # scoped | broad

    # ── coverage + priority over the reranked union (all opt-in) ───────────────
    # Pure reranking is collection-blind: one lexically-strong collection can take
    # every slot and starve the rest. These knobs layer a *soft* coverage floor and
    # a priority bias on top of relevance, applied AFTER reranking so the reranker's
    # judgment within each collection is preserved. Defaults (0 / empty / None) =
    # pure-relevance behaviour, unchanged.
    #
    # ``min_per_collection`` guarantees each retrieved collection contributes at
    # least this many chunks (a floor, taken from its best-reranked hits);
    # ``min_per_collection_overrides`` raises it per collection (e.g. reference: 2).
    # Floors are soft: an empty/weak collection leaves its slots to the general
    # pool — never padded. If the floors sum past ``rerank_top_n`` they are granted
    # in priority order (weight desc, then config order) until the budget is spent.
    min_per_collection: int = 0
    min_per_collection_overrides: dict[str, int] = Field(default_factory=dict)
    # Ranking bias: final_score = rerank_score * weight (default 1.0). A gentle
    # nudge to surface authoritative sources (e.g. reference: 1.2) first in the
    # citation order — keep it small; large weights defeat the reranker.
    collection_weights: dict[str, float] = Field(default_factory=dict)
    # Relevance floor for filling a reserved slot: a chunk below this rerank score
    # is never force-included by a quota. None disables it (reranker score scales
    # vary — tune once you've seen your reranker's distribution).
    min_score: float | None = None


class ChatQueryTransformConfig(BaseModel):
    """The advanced query-understanding stage. Each technique is independently
    gated so the pipeline degrades to plain retrieval by flipping one flag.

    - ``condense_history`` rewrites a follow-up into a standalone query using the
      conversation so far (so "what about grade 7?" retrieves correctly).
    - ``expansions`` generates N paraphrase/sub-question variants (multi-query);
      0 disables.
    - ``hyde`` embeds a hypothetical answer instead of the raw question."""

    provider: str = "llm"  # passthrough | llm
    condense_history: bool = True
    expansions: int = 3
    hyde: bool = False


class ChatContextConfig(BaseModel):
    """How retrieved chunks are packed into the synthesis prompt. ``max_chars``
    caps the grounding budget; ``compression`` runs an LLM sentence-level
    extraction pass to keep only query-relevant sentences before packing."""

    max_chars: int = 6000
    compression: bool = False


class ChatMemoryConfig(BaseModel):
    """Long-context handling. The last ``window_turns`` turns are kept verbatim;
    when history grows past ``summary_trigger_turns`` and ``summarize`` is on,
    the overflow is rolled into a running summary stored on the conversation."""

    window_turns: int = 8
    summarize: bool = True
    summary_trigger_turns: int = 12


class ChatSynthesisConfig(BaseModel):
    """The answer-generation stage. ``require_citations`` instructs the model to
    ground every claim in the numbered context and cite it ``[n]``."""

    temperature: float = 0.3
    require_citations: bool = True


class ChatConfig(BaseModel):
    """The QA chatbot subsystem — a second product surface over the same corpus.
    Every stage is config-tuneable; secrets/DSNs stay env-driven under
    ``store.url``. Omit the whole block to run with sensible defaults."""

    store: ChatStoreConfig = Field(default_factory=ChatStoreConfig)
    retrieval: ChatRetrievalConfig = Field(default_factory=ChatRetrievalConfig)
    query_transform: ChatQueryTransformConfig = Field(default_factory=ChatQueryTransformConfig)
    context: ChatContextConfig = Field(default_factory=ChatContextConfig)
    memory: ChatMemoryConfig = Field(default_factory=ChatMemoryConfig)
    synthesis: ChatSynthesisConfig = Field(default_factory=ChatSynthesisConfig)


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
    # Lexical encoder for hybrid search. Optional — when absent (and hybrid is on)
    # a sensible BM25 default is built. Same ``provider``/``model`` block shape.
    sparse_embedding: SparseEmbeddingConfig | None = None
    reranker: RerankerConfig
    vector_store: VectorStoreConfig
    retrieval: RetrievalConfig = Field(default_factory=RetrievalConfig)
    chunking: ChunkingConfig = Field(default_factory=ChunkingConfig)
    grounding: GroundingConfig = Field(default_factory=GroundingConfig)
    planner: PlannerConfig = Field(default_factory=PlannerConfig)
    intake: IntakeConfig = Field(default_factory=IntakeConfig)
    generation: GenerationConfig = Field(default_factory=GenerationConfig)
    critique: CritiqueConfig = Field(default_factory=CritiqueConfig)
    refine: RefineConfig = Field(default_factory=RefineConfig)
    profile: ProfileConfig = Field(default_factory=ProfileConfig)
    export: ExportConfig = Field(default_factory=ExportConfig)
    # The document/version memory layer — persists generated lessons/units and their
    # edit history so a teacher can revisit, edit, and undo. Optional; defaults to
    # an in-memory store so existing call sites/tests need no backend.
    documents: DocumentStoreConfig = Field(default_factory=DocumentStoreConfig)
    # The QA chatbot subsystem (streaming RAG over the collections). Optional —
    # defaults give a working chatbot with sqlite persistence and no extra infra.
    chat: ChatConfig = Field(default_factory=ChatConfig)

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
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    raw = _interpolate(raw)
    # BaseSettings merges env (LF__...) on top of the values passed in.
    return Settings(**raw)
