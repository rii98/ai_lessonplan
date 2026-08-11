# Corpus

Grounding content for the RAG collections (SRD § 06). Ingested into the vector
store by:

```bash
python -m lessonforge.rag.ingest --seed
```

## `seed/` — authored starter corpus (⚠️ not official CDC/NEB text)

The four `seed/<collection>.jsonl` files are a **small, authored seed corpus**
written to exercise the pipeline end-to-end and to ground the v1 reference topic
(*Components of Environment — Biotic & Abiotic*, Grade 6 Science). They are
**derived from the reference lesson (`lessonplan_reference/lp1.md`) and general
pedagogy — they are NOT the official Nepal CDC/NEB curriculum**. Standard codes
like `SEED-SC6-ENV-1` are placeholders, deliberately prefixed `SEED-` so they are
never mistaken for real CDC codes. Every record carries an honest `source` label
that flows into a lesson's `quality.grounding_sources`.

| File | Collection | What it grounds |
|------|-----------|-----------------|
| `curriculum.jsonl`    | `curriculum`    | learning-outcome phrasing, prior knowledge |
| `pedagogical.jsonl`   | `pedagogical`   | misconceptions + 5E strategies (the moat) |
| `exemplar.jsonl`      | `exemplar`      | chunks of the reference 5E lesson |
| `local_context.jsonl` | `local_context` | paddy field, goat, mushroom, river, monsoon… |

### JSONL record shape

One JSON object per line; `text` is the only required field:

```json
{"text": "…", "source": "…", "grade": 6, "subject": "Science", "standard": "…", "topic": "…"}
```

`grade`/`subject`/`standard`/`topic` become filterable metadata on the vector
record. `source` is the provenance label. `id` and `source` default sensibly if
omitted.

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
