# Corpus

Grounding content for the RAG collections (SRD § 06). Ingested into the vector
store by:

```bash
python -m lessonforge.rag.ingest --seed
```

## The four collections — what goes where

The knowledge base is split into **four collections**. Each answers a different
question the AI asks while writing a lesson, so putting a record in the right
collection is what makes it actually get used. Add content through the `/corpus`
page (a guided form — no JSON needed) or as JSONL here; the guidance is the same
either way.

| Collection | Put here… | Good example |
|------------|-----------|--------------|
| **`curriculum`** | **What students must learn** — official CDC/NEB learning outcomes, objectives, and the prior knowledge a grade is expected to have. The *what*, not the *how*. | *"By the end of Grade 6, students classify components of the environment as biotic or abiotic."* |
| **`pedagogical`** | **Teaching know-how — the moat.** Two kinds: (1) **misconceptions** students hold and how to correct them, and (2) **teaching strategies / moves** for a phase. The richer this is, the less generic every lesson becomes. | *"Misconception: students think anything that moves is alive, so they call clouds 'living'. Correction: movement isn't life — probe 'Can it grow, feed, and reproduce?'"* |
| **`exemplar`** | **Whole strong reference lessons**, added phase-by-phase, used as worked examples of what a great lesson looks like. Add complete lessons, not fragments. **Tag each with its `framework`** (see below). | *"Engage (संलग्न गराउनु), 5 min: ask 'What did you see on your way to school today?' and list answers on the board…"* |
| **`local_context`** | **Local hooks** that make a lesson feel Nepali and concrete — places, crops, animals, festivals, daily life. Flavour, not facts. | *"In the paddy fields around the village, frogs, snails and herons form a simple food chain."* |

**Which collection when in doubt?** Is it *what to learn* → `curriculum`. *How to
teach it / a misconception* → `pedagogical`. *A full model lesson* → `exemplar`.
*A local place or object to mention* → `local_context`.

### Frameworks: only `exemplar` is framework-specific

A lesson's **framework** (`5E`, `gradual_release`, `inquiry`) prescribes its phase
sequence. The AI already knows the exact phases for each framework (it never
copies 5E for an inquiry lesson) — that guarantee is built in, not learned from
the corpus. The corpus's job is to *flavour* with real examples:

- **Tag every `exemplar` with a `framework`** so a `gradual_release` lesson is
  shown gradual-release examples, not 5E ones. In the `/corpus` form, pick the
  Framework; in JSONL, add `"framework": "gradual_release"`. An untagged exemplar
  is treated as framework-agnostic and may surface for any framework.
- **`pedagogical` is *not* filtered by framework** — a misconception about clouds
  is true no matter which framework you pick, so misconceptions flow to every
  lesson. You *may* still tag a framework-specific teaching *strategy* with a
  `framework` for your own organisation, but you don't have to.
- **`curriculum` and `local_context` are framework-agnostic** — never tag them.

So: to support a new framework well, seed a **whole `exemplar` lesson tagged with
that framework** (and optionally a strategy note or two in `pedagogical`). You do
*not* need to duplicate curriculum or local context per framework.

## `seed/` — authored starter corpus (⚠️ not official CDC/NEB text)

The four `seed/<collection>.jsonl` files are a **small, authored seed corpus**
written to exercise the pipeline end-to-end and to ground the v1 reference topics.
They are **derived from the reference lesson (`lessonplan_reference/lp1.md`) and
general pedagogy — they are NOT the official Nepal CDC/NEB curriculum**. Standard
codes like `SEED-SC6-ENV-1` are placeholders, deliberately prefixed `SEED-` so
they are never mistaken for real CDC codes. Every record carries an honest
`source` label that flows into a lesson's `quality.grounding_sources`.

Seeded topics, so you can see each framework grounded end-to-end:

| Framework | Seed exemplar lesson | Subject |
|-----------|----------------------|---------|
| `5E`              | Components of Environment (Biotic & Abiotic) | Grade 6 Science |
| `gradual_release` | Area of a Rectangle (I do / We do / You do)  | Grade 6 Mathematics |
| `inquiry`         | What Seeds Need to Germinate                 | Grade 6 Science |

### JSONL record shape

One JSON object per line; `text` is the only required field:

```json
{"text": "…", "source": "…", "grade": 6, "subject": "Science", "standard": "…", "topic": "…", "framework": "5E"}
```

`grade`/`subject`/`standard`/`topic`/`framework` become filterable metadata on the
vector record. `source` is the provenance label. `framework` matters only for
`exemplar` records. `id` and `source` default sensibly if omitted.

## Extending to a real corpus (options 2 & 3)

The loader is pluggable (`rag/loaders.py`), so a real corpus is a drop-in:

- **Raw text / Markdown** — ingest a file directly:
  ```bash
  python -m lessonforge.rag.ingest --source path/to/lesson.md \
      --collection exemplar --format markdown --grade 6 --subject Science
  ```
- **CDC/NEB PDFs** — add a `pdf` loader (and an OCR path for scanned pages)
  under `rag/loaders.py` with `@register_loader("pdf")`; nothing else changes.
- **Empty corpus** — skip ingestion entirely; enrichment degrades gracefully and
  generation still works, just less grounded.
