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

Pipeline:  brief ─► [enrich + retrieve] ─► LDD ─► (validate) ─► artifacts
```

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
  generation, API. All run in-process via in-memory fakes.
- **Contract** (`tests/contract/`) — verify a real adapter honors its interface;
  gated behind `RUN_INTEGRATION=1`.

## Layout

```
config/config.yaml              single source of truth for wiring
src/lessonforge/
  config.py                     Settings loader (YAML + env override)
  container.py                  composition root (DI)
  domain/ldd.py                 the LDD + anti-generic validators
  providers/
    base.py                     the four interfaces (ports)
    registry.py                 provider string → adapter class
    llm/ embedding/ reranking/ vectorstore/   adapters
  rag/retriever.py              embed → search → rerank
  services/generation.py        brief → validated LDD (enrichment stage)
  export/
    base.py                     the Renderer port + ExportOptions
    registry.py                 (artifact, format) → renderer class
    markdown.py docx_render.py pptx_render.py   the renderers
    service.py                  config-driven compile + one-click zip bundle
  api/                          FastAPI app + DI
tests/                          unit + contract
tests/golden/                   byte-stable renderer fixtures
```

## Status

- **M1** — pluggable infrastructure + brief → validated LDD, verified end-to-end
  against `gemma4:31b-cloud`, Qdrant, and FastEmbed.
- **M3** — full export bundle: LDD → Word 5E plan, hook-first PPTX deck,
  worksheet + answer key, quiz, one-click zip. Devanagari de-risked; timing
  surfaced. Renderers sit behind a `(kind, format)` registry, swappable from
  config just like the providers.

Next per the roadmap (`roadmap.md`): M2 RAG ingestion of the CDC corpus and the
M4 critique→revise loop.
