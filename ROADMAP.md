# LessonForge — Roadmap & Progress

Living status doc. Update it as work lands. Companion to [`SRD.md`](SRD.md)
(the design) and [`README.md`](README.md) (how to run).

- **Legend:** ✅ done & verified · 🟡 in progress · ⬜ not started
- **Last updated:** 2026-08-11 (M4.5 edit-before-export UI + TeacherProfile)

---

## Where we are

**Milestone M1 — pluggable vertical slice: `brief → validated LDD` — ✅ done & verified.**
The full swappable infrastructure plus an end-to-end generation path, proven
against real backends (Ollama `gemma4:31b-cloud`, Qdrant, FastEmbed) and inside
Docker.

**Milestone M3 — full export bundle: `LDD → DOCX/PPTX/worksheet/quiz/zip` — ✅ done
& verified.** A `Renderer` port + `(kind, format)` registry (same loose-coupling
pattern as the providers) compiles a validated LDD into a Word 5E plan, a
hook-first PPTX deck, a worksheet + answer key, a quiz, and a one-click zip.
Devanagari de-risked (complex-script font hint verified in the DOCX/PPTX XML);
timing surfaced per phase. **68 unit tests + 1 live contract test green; ruff
clean; 99% coverage on the export module.**

**Milestone M2 — grounding: real ingestion + retrieval feeding enrichment — ✅ done
& verified.** A pluggable ingestion pipeline (load → chunk → embed → upsert) fills
four collections (curriculum, pedagogical, exemplar, local_context) from an authored
seed corpus; enrichment retrieves across them with grade/subject filters and stamps
**authoritative provenance** into the LDD. Loaders and grounding strategy are both
config-swappable. **101 unit tests + 2 live contract tests green; ruff clean.**

**Milestone M4 (backend core) — quality & editing pipeline — ✅ done & verified.**
The pipeline is now `intake → enrich → critique & revise`, each stage a config-
swappable port. A `Critic` scores the draft LDD on the anti-generic rubric
(engagement, alignment, misconception coverage, specificity, local relevance);
the reviser rewrites weak sections until the weighted score clears the threshold
or the budget hits, then stamps the scores into `quality`. Intake normalizes a
raw request — or a **pasted existing plan (US-3)** — into a brief and lets
enrichment *enrich* it rather than replace it. An **eval harness** scores a fixed
golden set (structural + rubric) and gates CI on it. Per-stage model selection
(fast model for intake, reasoning model for enrichment/critique) is wired.
**148 unit tests + 2 live contract tests green; ruff clean; 93% total coverage
(97–100% on the new M4 modules).**

**Milestone M4.5 — edit-before-export UI + `TeacherProfile` — ✅ done & verified.**
A single self-contained, low-bandwidth web editor served at `/` (vanilla JS, no
build): generate → **edit the whole LDD in place** → check guardrails → export.
`POST /lessons/validate` validates an edited LDD and returns field-level errors
with HTTP 200 (a normal editing state, not a failed request), so violations pin
to their field. **`TeacherProfile`** carries stored preferences — defaults that
fill only unset request fields (explicit always wins) plus a teaching voice +
local anchors — injected into every build by the pipeline, so intake and
generation stay profile-agnostic. Preferences persist behind a pluggable
`ProfileStore` port (`memory` default, `file` for JSON-per-teacher). **172 unit
tests + 2 live contract tests green; ruff clean; eval gate PASS; 99% coverage on
the new modules. Browser-verified end-to-end (render → generate → edit → validate
→ DOCX download).**

---

## Done ✅

### Architecture & infra
- ✅ Config-driven provider system: one line in `config/config.yaml` swaps any backend
- ✅ Abstract interfaces (`providers/base.py`): `LLMClient`, `Embedder`, `Reranker`, `VectorStore`
- ✅ Provider registry + `build_*` factories; unknown provider fails loudly at startup
- ✅ DI composition root (`container.py`)
- ✅ Settings loader: YAML + `${VAR:-default}` interpolation + env override (`LF__…`)
- ✅ Robust config path resolution (works in src-layout dev and installed/Docker)

### Adapters
- ✅ LLM: Ollama (`llm/ollama.py`) — incl. lenient JSON parsing for fenced output
- ✅ Embeddings: FastEmbed (`embedding/fastembed.py`), lazy model load
- ✅ Reranker: FastEmbed (default), HTTP (FastAPI service), no-op
- ✅ Vector store: Qdrant (`vectorstore/qdrant.py`), string-ID → UUID5 mapping

### Domain & pipeline
- ✅ Lesson Design Document + anti-generic validators (hook-not-definition, every
  objective taught **and** assessed, MCQ needs options, unique IDs)
- ✅ Retriever: embed → search → rerank (interface-only deps)
- ✅ Generation service: brief → LDD, few-shot shape-anchored (defeats the model
  ignoring `format`), validation gate rejects generic output

### API, Docker, tests
- ✅ FastAPI: `/health`, `/health/providers`, `POST /lessons/generate`
- ✅ Dockerfile + docker-compose (Qdrant + api; Ollama on host); build verified
- ✅ Tests: config, registry-swap, LDD guardrails, retriever, generation, API
- ✅ Live contract test (Ollama) gated by `RUN_INTEGRATION=1`
- ✅ Ruff clean

---

## Remaining ⬜

### M2 — Grounding (make retrieval real) — ✅ done & verified
- ✅ Ingestion pipeline: load → chunk → embed → upsert; idempotent (content-hash ids)
- ✅ Collections: `curriculum` (CDC/NEB), `pedagogical`, `exemplar`, `local_context`
- ✅ Pluggable loaders (`jsonl`, `markdown`) behind a registry — CDC PDFs/OCR = one new
  class later (options 2/3), zero downstream change
- ✅ Authored **seed corpus** (`corpus/seed/*.jsonl`), clearly labeled *not* official CDC
  text; derived from the reference lesson + Grade-6 science pedagogy. `SEED-` codes only.
  *(Sourcing/licensing the real CDC corpus remains an open decision — see below — but no
  longer blocks the pipeline.)*
- ✅ Enrichment wired to real multi-collection retrieval; **provenance is authoritative** —
  the retrieved sources overwrite any the model invents in `quality.grounding_sources`
- ✅ Metadata filters (grade/subject) on search, with a lenient fallback (broaden before
  returning nothing). Config-driven via the `grounding:` block.
- ✅ `python -m lessonforge.rag.ingest --seed` (and `--source … --collection …`)
- ✅ Live ingest+retrieve contract test (Qdrant + FastEmbed) gated by `RUN_INTEGRATION`;
  101 unit tests green, ruff clean, 97% coverage on the RAG module

### M3 — Full export bundle — ✅ done & verified
- ✅ DOCX lesson plan renderer (reproduces `lp1.md` layout: 5E table, differentiation, homework)
- ✅ **Devanagari rendering** verified in DOCX/PPTX — complex-script font hint (`w:cs` /
  `a:cs`) set on every run; tests assert codepoints + font hint survive into the XML.
  (PDF deferred — no PDF renderer yet; add a `pdf` format later, no interface change.)
- ✅ PPTX slide deck renderer (hook-first: title → hook → objectives → phases → check)
- ✅ Worksheet renderer + answer key (on its own page)
- ✅ Quiz renderer (MCQ/TF/short) + export endpoints + one-click zip bundle
- ✅ Golden-file tests: byte-stable Markdown renderers assert exact bytes; DOCX/PPTX
  verified structurally (open + inspect); zip metadata pinned for determinism
- ✅ Timing surfaced in output: per-phase minutes vs target, over/under-run flagged
- ✅ Loosely coupled: `Renderer` port + `(kind, format)` registry mirroring the provider
  system; `export:` config block swaps a format in one line (or `LF__EXPORT__…` env)

### M4 — Quality & editing
- ✅ Critique → revise loop: `Critic` port (`structural` | `llm` | `composite` | `noop`)
  scores the rubric; reviser rewrites weak sections to threshold/budget, stamps scores.
  Structural critic is deterministic (no LLM) — the default and the test seam.
- ✅ Intake stage: `Intake` port (`llm` | `heuristic`) parses a pasted existing plan →
  `NormalizedBrief` (US-3); enrichment enriches the draft rather than replacing it.
- ✅ Eval harness: ~30-topic golden set (`corpus/golden/`), structural + rubric scoring,
  `python -m lessonforge.eval` gate wired into CI (GitHub Actions).
- ✅ Per-stage model selection: optional `llm_fast` (cheap model for intake) vs `llm`
  (reasoning model for enrichment/critique) — one config block, no code change.
- ✅ CI workflow: ruff + unit tests + eval gate on every push/PR.

### M4.5 — Edit-before-export UI + teacher profile — ✅ done & verified
- ✅ Self-contained web editor at `GET /` (`api/static/index.html`): generate →
  edit the full LDD (objectives, hook, misconceptions, phases, checks, homework,
  differentiation) → check guardrails → export. Vanilla JS, no build step, one
  request; low-bandwidth first. Devanagari renders in the editor.
- ✅ `POST /lessons/validate`: validates an edited LDD against the anti-generic
  guardrails, returns `{valid, errors:[{loc, msg}]}` with **HTTP 200 even when
  invalid** so the UI shows field-level errors inline, not as a request failure.
- ✅ `TeacherProfile` (`domain/profile.py`): defaults fill only unset request
  fields (explicit wins); voice (`style_notes`) + `local_anchors` flavour every
  build. Applied by the pipeline (`run(request, profile=…)`) — intake/generation
  stay profile-agnostic. Multi-tenant-ready (`owner_id`).
- ✅ Pluggable `ProfileStore` port (`services/profile.py`): `memory` (default) |
  `file` (JSON-per-teacher, path-traversal-safe), behind the registry pattern;
  `profile:` config block. Real multi-tenant DB store = a later provider, no rewrite.
- ✅ `GET`/`PUT /profile` endpoints; `/generate` + `/intake` load & apply the profile.
- ✅ 24 new tests (`test_profile.py`, `test_profile_api.py`); 172 total green, ruff
  clean, eval gate PASS, 99% coverage on new modules; browser-verified end-to-end.

### M5 — School-ready
- ⬜ Data model & multi-tenancy (`org_id`/`owner_id` scoping)
- ⬜ Teacher personal corpus ("learns from past lessons")
- ⬜ School/department shared corpus + HoD standardization

### Cross-cutting / tech debt
- ⬜ Streaming progress per pipeline stage
- ⬜ Caching (retrieval + intermediate LDDs), token budgets
- ⬜ Additional LLM adapters (prove the swap: e.g. OpenAI-compatible)
- ✅ CI workflow (ruff + unit + eval gate; integration on demand via `RUN_INTEGRATION`)
- ⬜ Persistence for generated lessons

---

## Open decisions
- CDC/NEB corpus: source, format, licensing — *pipeline no longer blocked (seed corpus
  ships); still need the real corpus sourced/licensed to replace the seed for production*
- v1 grade/subject focus (reference is secondary science — start there?)
- Default language mode: EN body + NE terms, or full bilingual?
- Hosting & data residency for Nepal schools
- "Paste plan → enrich" flow: v1 headline or later?

---

## How progress is tracked
- **This file** — human-readable milestone status; update when work lands.
- **`SRD.md`** — the design/rationale (why), kept stable.
- **Cross-session memory** — durable facts for the assistant live in
  `.claude/projects/<project>/memory/` (project state, model quirks,
  preferences), indexed by `MEMORY.md`. Not committed to the repo.
