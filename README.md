# LessonForge — AI Lesson Plan Creator

> *AI that thinks like an experienced teacher.* Turns a topic into a validated
> **Lesson Design Document (LDD)** grounded in Nepal's CDC/NEB curriculum, from
> which every teaching artifact is compiled. See [`SRD.md`](SRD.md) for the
> full design rationale.

Everything swappable — **LLM, embedder, reranker, vector store** — sits behind
an interface and is chosen by one line in `config/config.yaml`. No code change
to swap a backend.

## Architecture at a glance

```
config/config.yaml ─► Settings ─► Container (DI) ─► FastAPI
                          │
   provider strings  ┌─── LLMClient   (ollama)      ─┐  every consumer depends
   resolved by the   ├─── Embedder    (fastembed)   ─┤  only on the ABC in
   registry to a     ├─── Reranker    (fastembed…)  ─┤  providers/base.py —
   concrete adapter  └─── VectorStore (qdrant)      ─┘  never the concrete class

Pipeline:  input ─► [intake] ─► brief ─► [enrich + retrieve] ─► draft LDD
                 ─► [critique + revise] ─► validated LDD ─► edit ─► artifacts
                    ▲ TeacherProfile: defaults + voice + local anchors
```

Every stage — intake, critic, reranker, renderer — is a port resolved by config,
so a strategy swap (`critique.provider: structural → llm`, `intake.provider:
llm → heuristic`) is a one-line change, never a code edit.

| Concern       | Default        | Swap to (config `provider:`)      |
|---------------|----------------|-----------------------------------|
| LLM           | `ollama`       | add an adapter, e.g. `openai`     |
| Embeddings    | `fastembed`    | add an adapter                    |
| Reranker      | `fastembed`    | `http` (FastAPI service), `noop`  |
| Vector store  | `qdrant`       | add an adapter                    |

## Requirements

- Python 3.11+
- Docker (for Qdrant)
- [Ollama](https://ollama.com) with the configured model:
  `ollama pull gemma4:31b-cloud` (cloud models: run `ollama signin` first)

## Quick start (local dev)

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"

docker compose up -d qdrant          # vector store
# Ollama runs on the host (ollama serve / desktop app)

python -m lessonforge.rag.ingest --seed   # load the grounding corpus into Qdrant

uvicorn lessonforge.api.main:app --reload
# → http://localhost:8000/docs
```

Generate a lesson:

```bash
curl -s http://localhost:8000/lessons/generate \
  -H 'content-type: application/json' \
  -d '{"topic":"Components of Environment: Biotic and Abiotic",
       "grade":6,"subject":"Science","duration_min":45}' | jq .
```

Check which backends are wired and reachable:

```bash
curl -s http://localhost:8000/health/providers | jq .
```

Export the generated LDD into teaching artifacts (Word plan, PPTX deck,
worksheet, quiz) — post an LDD back to an export endpoint, or grab the
one-click zip of everything:

```bash
# generate once, then compile every artifact into a zip bundle
curl -s http://localhost:8000/lessons/generate -H 'content-type: application/json' \
  -d '{"topic":"Components of Environment","grade":6,"subject":"Science"}' \
| curl -s http://localhost:8000/lessons/export/bundle/zip \
    -H 'content-type: application/json' --data-binary @- -o lesson_bundle.zip

# or one artifact, overriding the configured format
curl -s http://localhost:8000/lessons/export/lesson_plan?fmt=md \
  -H 'content-type: application/json' --data-binary @ldd.json

curl -s http://localhost:8000/export/manifest | jq .   # what can be produced
```

Each artifact's default format is one line in `config/config.yaml` under
`export:` (or `LF__EXPORT__SLIDES=…`); the `(kind, format)` registry resolves it,
so adding a `pdf` renderer never touches the service or API. Nepali (Devanagari)
text is embedded with a complex-script font hint so it renders in Word/PowerPoint.

## Grounding (RAG)

Enrichment retrieves from four collections — `curriculum`, `pedagogical`,
`exemplar`, `local_context` — and stamps the **real retrieved sources** into the
lesson's `quality.grounding_sources` (a model can't invent a citation; the
retrieved provenance wins).

```bash
python -m lessonforge.rag.ingest --seed         # ingest corpus/seed/*.jsonl
# or a single source into a chosen collection:
python -m lessonforge.rag.ingest --source lessonplan_reference/lp1.md \
    --collection exemplar --format markdown --grade 6 --subject Science
```

**Knowledge-base UI (`/corpus`).** The corpus is the moat, and it shouldn't need a
shell to grow. A self-contained page — linked from the editor header — lets a
curator **add** records (a guided single-record form, or bulk JSONL paste/upload),
**browse** what's stored per collection, and **delete** bad records. Ingestion is
idempotent (records key on their content hash), so re-submitting the same text
updates in place rather than duplicating. It drives four endpoints:

```bash
# what's in each collection (name + curator description + live count)
curl -s http://localhost:8000/corpus/overview | jq .

# add/update records (same rules as a .jsonl file; a record may set its own collection)
curl -s http://localhost:8000/corpus/ingest -H 'content-type: application/json' -d '{
  "collection": "pedagogical",
  "records": [{"text": "Soil is abiotic, though it teems with living things.", "grade": 6, "subject": "Science"}]
}' | jq .

# browse (paginated) and delete by id
curl -s "http://localhost:8000/corpus/collections/pedagogical/records?limit=25" | jq .
curl -s http://localhost:8000/corpus/collections/pedagogical/delete \
  -H 'content-type: application/json' -d '{"ids": ["<record-id>"]}' | jq .
```

The shipped corpus under `corpus/seed/` is a small **authored seed — not official
CDC/NEB text** (see [`corpus/README.md`](corpus/README.md)). Loaders are pluggable
(`jsonl`, `markdown` today; a `pdf`/OCR loader is one `@register_loader` class
away), and which collections enrichment queries, how much each contributes, and
which brief fields become metadata filters are all set in the `grounding:` config
block. With an empty corpus, retrieval degrades gracefully and generation still
works — just less grounded.

## Quality: intake, critique → revise, and the eval gate

`POST /lessons/generate` runs the full pipeline: **intake → enrichment →
critique & revise**. Each stage is a config-swappable port.

**Intake (US-3).** Paste a rough existing plan and the tool *enriches* it instead
of starting over — intake pulls out grade/subject/duration/topic and hands the
draft to enrichment:

```bash
curl -s http://localhost:8000/lessons/generate -H 'content-type: application/json' \
  -d '{"existing_plan":"Class 7 Science, 40 min. Topic: Sound. We read the book and copy notes."}'

curl -s http://localhost:8000/lessons/intake -H 'content-type: application/json' \
  -d '{"existing_plan":"..."}' | jq .   # preview what intake parsed, before generating
```

**Critique → revise.** A `Critic` scores the draft LDD on the anti-generic rubric
(engagement, alignment, misconception coverage, specificity, local relevance); the
reviser rewrites the weak sections until the weighted score clears `critique.threshold`
or `critique.max_iterations` runs out, then stamps the scores into `quality`. The
default critic is **`structural`** — deterministic, no LLM, no cost; switch to
`llm` or `composite` for an LLM judge. Score any LDD directly:

```bash
curl -s http://localhost:8000/lessons/critique -H 'content-type: application/json' \
  --data-binary @ldd.json | jq '{overall, scores}'
```

**Eval harness (quality as a number).** A fixed golden set is scored (structural +
rubric) and turned into a pass/fail gate — the acceptance gate for a prompt/model
change, run in CI:

```bash
python -m lessonforge.eval                    # deterministic gate over authored exemplars
RUN_INTEGRATION=1 python -m lessonforge.eval --generate   # live: golden topics → pipeline → score
```

**Per-stage models.** Intake uses a cheap fast model, enrichment/critique the
strong reasoning model — add an optional `llm_fast:` block to `config/config.yaml`
(same shape as `llm:`); omit it to reuse `llm` everywhere.

## Edit before export & teacher profile (web UI)

The plan you export is the plan you *see*. A single self-contained, low-bandwidth
page (vanilla JS, no build step) lets a teacher generate a lesson, **edit every
part of the LDD**, check the guardrails, and export — served straight off the API:

```bash
uvicorn lessonforge.api.main:app --reload
# → http://localhost:8000/   (the editor)   ·   /docs (the API)
```

Flow: enter a topic (or paste a plan) → **Generate** → edit objectives, hook,
misconceptions, phases, checks, homework in place → **Check guardrails** → export
each artifact or the zip. The edited LDD is the source of truth for every export.

**`POST /lessons/validate`** backs the guardrail check: it validates an edited LDD
against the anti-generic guardrails and returns `{valid, errors:[{loc, msg}]}` —
with **HTTP 200 even when invalid**, so the editor pins each violation (missing
hook, an objective never assessed, …) to its field instead of failing the request.

**`TeacherProfile`** — stored preferences injected into *every* build (SRD §11):

- **Defaults** (grade/subject/duration/language/framework) fill only the request
  fields you leave unset — an explicit choice always wins.
- **Personalization** — a teaching `style_notes` voice and favourite
  `local_anchors` — flavour the generated lesson so it feels like *yours*.

The profile is applied by the pipeline (`run(request, profile=…)`), so intake and
generation stay profile-agnostic. It lives behind a pluggable `ProfileStore` port —
`memory` (default) or `file` (one JSON per teacher, survives restarts) — swapped in
the `profile:` config block, the real multi-tenant DB store being a later provider,
not a rewrite.

```bash
curl -s -X PUT http://localhost:8000/profile -H 'content-type: application/json' \
  -d '{"default_grade":6,"default_subject":"Science",
       "style_notes":"warm, storytelling, lots of pair work",
       "local_anchors":["Phewa lake","millet farming"]}'
curl -s http://localhost:8000/profile | jq .
# now a bare topic inherits grade/subject and the voice + anchors:
curl -s http://localhost:8000/lessons/generate -H 'content-type: application/json' \
  -d '{"topic":"The Water Cycle"}' | jq '.curriculum_ref, .local_context'
```

## Full stack in Docker

```bash
docker compose up --build     # qdrant + api; api reaches host Ollama
```

The reranker and embedder run in-process (FastEmbed, no server). The only
external services are Qdrant and Ollama.

## Swapping a component (the whole point)

Edit `config/config.yaml` — one line:

```yaml
reranker:
  provider: http                       # was: fastembed
  endpoint: http://reranker:8100/rerank
```

or override without touching files:

```bash
LF__LLM__MODEL=gpt-oss:120b-cloud uvicorn lessonforge.api.main:app
```

An unknown provider fails **loudly at startup** with the list of available ones.

## Testing

```bash
pytest                       # unit tests — no external services, no model downloads
RUN_INTEGRATION=1 pytest     # + live contract tests (needs Qdrant + Ollama)
```

- **Unit** — config precedence, registry swapping, LDD guardrails, retriever,
  generation, intake, critique, revise loop, pipeline, eval harness, teacher
  profile + store, edit/validate + profile API, the served editor page, and the
  corpus manager (ingest records → browse → delete). All run in-process via
  in-memory fakes (179 tests).
- **Contract** (`tests/contract/`) — verify a real adapter honors its interface;
  gated behind `RUN_INTEGRATION=1`.
- **Eval gate** — `python -m lessonforge.eval` scores the golden set and exits
  non-zero below threshold; runs in CI (`.github/workflows/ci.yml`) after lint + unit.

## Layout

```
config/config.yaml              single source of truth for wiring
src/lessonforge/
  config.py                     Settings loader (YAML + env override)
  container.py                  composition root (DI)
  domain/ldd.py                 the LDD + anti-generic validators
  domain/profile.py             TeacherProfile: defaults + voice/anchors, applied to a build
  providers/
    base.py                     the four interfaces (ports)
    registry.py                 provider string → adapter class
    llm/ embedding/ reranking/ vectorstore/   adapters
  rag/
    documents.py                Collection enum, Document/Chunk, content-hash ids
    loaders.py                  pluggable source loaders (jsonl, markdown)
    chunkers.py                 deterministic paragraph chunker
    ingest.py                   load→chunk→embed→upsert + ingest CLI
    retriever.py                embed → search → rerank
    grounding.py                multi-collection retrieval + provenance
  domain/rubric.py              the critique rubric (scores) as data
  services/
    intake.py                   raw request / pasted plan → NormalizedBrief (US-3)
    generation.py               brief → draft LDD (enrichment stage)
    critique.py                 Critic port + structural/llm/composite/noop scorers
    revise.py                   critique → rewrite weak sections → validated LDD
    pipeline.py                 composes intake → enrich → critique/revise (+ profile)
    profile.py                  ProfileStore port (memory | file) for teacher prefs
    registry.py                 stage provider string → intake/critic/profile-store class
  eval/                         golden-set scoring + `python -m lessonforge.eval` gate
  export/
    base.py                     the Renderer port + ExportOptions
    registry.py                 (artifact, format) → renderer class
    markdown.py docx_render.py pptx_render.py   the renderers
    service.py                  config-driven compile + one-click zip bundle
  api/                          FastAPI app + DI
    static/index.html           the self-contained edit-before-export web editor
    static/corpus.html          the self-contained knowledge-base manager (/corpus)
corpus/seed/                    authored seed grounding corpus (JSONL)
tests/                          unit + contract
tests/golden/                   byte-stable renderer fixtures
```

## Status

- **M1** — pluggable infrastructure + brief → validated LDD, verified end-to-end
  against `gemma4:31b-cloud`, Qdrant, and FastEmbed.
- **M2** — grounding: pluggable ingestion (load→chunk→embed→upsert) fills four
  collections from an authored seed corpus; enrichment retrieves across them with
  grade/subject filters and stamps authoritative provenance into the LDD.
- **M3** — full export bundle: LDD → Word 5E plan, hook-first PPTX deck,
  worksheet + answer key, quiz, one-click zip. Devanagari de-risked; timing
  surfaced. Renderers sit behind a `(kind, format)` registry, swappable from
  config just like the providers.
- **M4 (backend core)** — quality & editing pipeline: intake (US-3 paste-a-plan),
  critique→revise loop on the anti-generic rubric, eval harness + CI gate, and
  per-stage model selection. Every stage a config-swappable port.
- **M4.5 (edit-before-export UI + `TeacherProfile`)** — a self-contained web editor
  at `/` (generate → edit the whole LDD → check guardrails → export), a
  `POST /lessons/validate` guardrail check, and stored teacher preferences
  (defaults + voice + local anchors) injected into every build behind a pluggable
  `ProfileStore` (`memory` | `file`). Browser-verified end-to-end.
- **M4.6 (knowledge-base manager)** — a self-contained `/corpus` page to grow and
  curate the grounding corpus without the CLI: add via a guided form or bulk JSONL,
  browse records per collection, and delete bad ones. Adds read/curation methods
  (`count`, `scroll`, `delete`) to the `VectorStore` port and a `/corpus/*` API;
  ingestion stays idempotent, so add-vs-update needs no new logic.

Next per the roadmap (`ROADMAP.md`): source/license the real CDC corpus to replace
the seed, then M5 (school-ready: multi-tenancy + personal/shared corpora).
