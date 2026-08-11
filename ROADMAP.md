# LessonForge — Roadmap & Progress

Living status doc. Update it as work lands. Companion to [`SRD.md`](SRD.md)
(the design) and [`README.md`](README.md) (how to run).

- **Legend:** ✅ done & verified · 🟡 in progress · ⬜ not started
- **Last updated:** 2026-08-11

---

## Where we are

**Milestone M1 — pluggable vertical slice: `brief → validated LDD` — ✅ done & verified.**
The full swappable infrastructure plus an end-to-end generation path, proven
against real backends (Ollama `gemma4:31b-cloud`, Qdrant, FastEmbed) and inside
Docker. 31 unit tests + 1 live contract test green.

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

### M2 — Grounding (make retrieval real)
- ⬜ Ingestion pipeline: chunk → embed → upsert into Qdrant collections
- ⬜ Collections: `curriculum` (CDC/NEB), `pedagogical`, `exemplar`, `local_context`
- ⬜ Source & license the CDC/NEB corpus (machine-readable? OCR from PDFs?) — **decision needed**
- ⬜ Wire enrichment to real retrieval + record `grounding_sources` provenance
- ⬜ Metadata filters (grade/subject/standard) on search

### M3 — Full export bundle (most motivating next step)
- ⬜ DOCX lesson plan renderer (reproduce `lp1.md` layout: 5E table, closure, homework)
- ⬜ **Devanagari rendering** verified in DOCX/PPTX/PDF — *de-risk early*
- ⬜ PPTX slide deck renderer (hook-first)
- ⬜ Worksheet renderer + answer key
- ⬜ Quiz renderer (MCQ/TF/short) + export endpoints + one-click bundle
- ⬜ Golden-file tests for renderers (fixed LDD → stable bytes)
- ⬜ Timing variants (30/45/60) surfaced in output

### M4 — Quality & editing
- ⬜ Critique → revise loop (rubric scoring, rewrite weak sections)
- ⬜ Intake stage: parse pasted existing plan → NormalizedBrief (US-3)
- ⬜ Eval harness: ~30-topic golden set, rubric + structural scoring in CI
- ⬜ Web UI for edit-before-export; `TeacherProfile` preferences

### M5 — School-ready
- ⬜ Data model & multi-tenancy (`org_id`/`owner_id` scoping)
- ⬜ Teacher personal corpus ("learns from past lessons")
- ⬜ School/department shared corpus + HoD standardization

### Cross-cutting / tech debt
- ⬜ Streaming progress per pipeline stage
- ⬜ Caching (retrieval + intermediate LDDs), token budgets
- ⬜ Additional LLM adapters (prove the swap: e.g. OpenAI-compatible)
- ⬜ CI workflow (lint + unit; integration on demand)
- ⬜ Persistence for generated lessons

---

## Open decisions
- CDC/NEB corpus: source, format, licensing → gates M2
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
