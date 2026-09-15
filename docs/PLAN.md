# LessonForge: Multi-Day Units + Smart Retrieval + Memory

Living plan for the plan-and-expand unit feature, adaptive retrieval, and the
document/version memory layer. Each phase is independently shippable and tested.

## Guiding principles
- Spend intelligence at ingest; keep query-time cheap.
- Reuse the LDD as the per-day source of truth — a Unit *composes* LDDs.
- Every new decision is a swappable, config-gated provider that degrades cleanly.
- Measure before you enforce (shadow-log scores before acting on them).
- Small validated plans gate expensive generation.

## Dependency order
```
Phase 0  Quick correctness wins  ──┐
Phase 1  Retrieval foundation    ──┼─► Phase 4  Multi-day units
Phase 2  Adaptive router + gate  ──┘        ▲
Phase 3  Document store + history ──────────┘
```

## Status

### Phase 0 — Correctness quick wins ✅ DONE
- [x] Inject grounding into the refine prompt (retrieve once, use twice) — `services/refine.py`
- [x] Shadow-log reranker scores at DEBUG — `rag/retriever.py`
- Tests: `tests/test_refine.py` (grounding reaches prompt; sources merge not replace; empty bundle adds no block).

### Phase 1 — Retrieval foundation: multi-granularity (no LLM) ✅ DONE
- [x] 1a. Hierarchy ids at ingest — `doc_id` (ingestor), `chunk_index` (base chunker), `chapter`+`heading_path` (markdown chunker). Filter on `{doc_id, heading_path/chapter}`; no hashing.
- [x] 1b. Granularity-aware retriever (narrow | section | broad) via `VectorStore.fetch` deref + reassembly, budget-capped, dedup, flat-source fallback. `GroundingRetriever.ground(granularity=...)` honors it for authoritative collections only.
- [x] 1c. `GroundingRetriever.skeleton()` — free heading table-of-contents (no LLM).
- Tests: `tests/test_granularity_retrieval.py` (11).

### Phase 2 — Adaptive router + quality gate ✅ DONE
- [x] 2a. `rag/planner.py` — `RetrievalPlanner` (heuristic | llm | hybrid), kind- and stem-based, LLM-degrades-to-heuristic, `_CachingPlanner` memoizes by (kind, need). Wired in container from `settings.planner`.
- [x] 2b. Quality gate in `GroundingRetriever`: top-anchored `min_score` garbage floor (recall-biased — never prunes marginal hits, only drops a collection whose best hit is weak) + optional fail-open LLM `verify` on the top authoritative hit. Both off by default.
- Tests: `tests/test_planner.py` (10), gate cases in `tests/test_reference_grounding.py` (5).

### Phase 3 — Document store + version history (memory layer) ✅ DONE
- [x] 3a. `services/documents/store.py` — `DocumentStore` (memory | sqlite | postgres), shared SQL logic, registered.
- [x] 3b. `domain/document.py` — `StoredDocument` + immutable `DocumentVersion` chain (parent_id) + head pointer + audit fields (origin/instruction/diff/sources/scores).
- [x] 3c. `services/documents/service.py` — `DocumentService` (save/commit_refine/commit_manual/undo/redo/rehydrate). API: `POST /documents/lessons`, `GET /documents`, `GET /documents/{id}[?version=]`, `GET /documents/{id}/versions`, `POST /documents/{id}/refine[?commit]`, `POST .../undo`, `.../redo`, `DELETE`. Container lazy getters.
- Tests: `test_document_store.py` (11×2 backends), `test_document_service.py` (9), `test_documents_api.py` (9).

### Phase 4 — Multi-day units (plan-and-expand) ✅ DONE
- [x] 4a. `domain/unit.py` — `UnitPlan` (spine, sequential-day validator) + `UnitDesignDocument` (cross-day validators: day count matches plan, shared grade/subject) + `CoherenceReport` + `UnitRequest`.
- [x] 4b. `services/unit_planner.py` — `UnitPlanner`: one grounded (broad, via retrieval planner) LLM call → validated spine; renumbers days, fills curriculum_ref.
- [x] 4c. `services/unit_generation.py` — `UnitGenerator`: sequential day expansion via the existing `LessonGenerator`, arc-aware through `NormalizedBrief.unit_context` (new field + generation prompt block); `regenerate_day` rebuilds one day only.
- [x] 4d. Per-day critique/revise reused (`Reviser`), unchanged.
- [x] 4e. `services/unit_coherence.py` — deterministic report (duplicate_topic, repeated_hook, minute_budget, outcome_gap, continuity). (Targeted LLM repair deferred to Phase 5.)
- [x] 4f. `ExportService.unit_zip` (per-day lesson plans) + unit persistence in `DocumentService` (save_unit/head_unit/commit_unit) + API: `POST /units/plan`, `/units/generate`, `GET /units/{id}`, `POST /units/{id}/days/{n}/regenerate`, `POST /units/{id}/export`.
- Tests: `test_unit_generation.py` (8), `test_unit_coherence.py` (10), `test_units_api.py` (8).

## Totals
496 tests passing (was 408 at session start; +88 across phases 0-4). Lint clean.

### Phase 5 — Polish (only if needed)
- [ ] Ingest section summaries (RAPTOR roll-up, llm_fast, incremental)
- [ ] Compression ladder (fits → summaries → extractive → abstractive cached)
- [ ] Per-stage fast model wiring for planner/verify/summaries
- [ ] **Arc-aware day refine (deferred — measure first).** `/lessons/refine` on a
  unit day re-grounds against the day's topic but is NOT arc-aware (`unit_context`
  lives on `NormalizedBrief`, not on the LDD — deliberate layering). The fallout is
  already *detected*: `PUT /units/{id}/days/{day}` re-runs `UnitCoherence` on save
  and the editor now surfaces the recomputed report (coherence-after-save). Only if
  post-refine coherence notes fire often in practice, promote to a thin unit-scoped
  refine that reuses `UnitGenerator._day_brief`'s arc-context builder as prompt +
  grounding context, leaving the core `Refiner` untouched.

### UI (plain HTML/JS, served by FastAPI — React migration deferred)
- [x] Multi-day unit planner page (`static/units.html`): editable spine preview →
  `POST /units/generate` with the edited `plan` (plan-passthrough) → per-day view,
  regenerate a day, coherence report, export zip.
- [x] Library page (`static/library.html`): saved lessons + units, version timeline,
  undo/redo/delete/export, deep-link `?doc=`.
- [x] Editor edit-mode (`static/index.html`): `/?doc=<id>` edits a saved lesson,
  `/?unit=<id>&day=<n>` edits one unit day; Save commits back
  (`POST /documents/{id}/manual`, `PUT /units/{id}/days/{day}`); coherence-after-save.
- [x] Shared `static/app.css` + `static/app.js`; nav across all pages.
