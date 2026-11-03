# Long-term memory (L3) — v2

LessonForge remembers a few things about each learner (how they like to learn, their
goals and interests, what they find hard, which lessons they've done) so answers and
lessons fit them. It also tracks **mastery** per knowledge component. This document
covers the L3 layer. Conversation memory (verbatim window + structured rolling summary)
and learn-mode session state are in `docs/LEARN_MODE.md` §7.

Code: `src/lessonforge/services/memory/` · domain: `domain/memory.py` · schema:
`db/migrations/0003_learning_and_memory.sql` + `0004_memory_v2.sql` · API:
`api/routers/memory.py` · tests: `tests/test_memory_layer.py`, `tests/test_memory_v2.py`.

---

## 1. What changed and why

v2 combines our v1 layer with the best parts of the Socratic tutor's memory v2. It
follows the pattern production assistants use: the LLM decides what to write, writes
happen in the background, and reads inject two layers.

| Concern | v1 (ours) | Socratic v2 | **v2 (now)** |
|---|---|---|---|
| Write decision | one LLM call did extract + compare, against items similar to the **raw message** | extract, then a separate reconcile, against items similar to **each candidate** | separate **extract → reconcile**, with neighbours found per candidate. All candidates are embedded in **one batched call** |
| Contradictions | `update` only | DELETE (hard) | **retire** (soft, kept as history) + `add` of the new fact. A user's "forget" stays a hard delete |
| LLM down / bad JSON | memory lost for that turn | retry once, then add unless near-duplicate | same, behind a `rules` reconciler. Aliases (`c1`/`m2`) mean the model **cannot invent ids** |
| Concurrency | single worker per process, nothing across replicas | asyncio lock + advisory lock held **across the LLM call** | **optimistic**: decide on a snapshot, then apply in one transaction under a per-learner advisory lock and **re-check every op at apply time**. No lock is held while an LLM runs |
| Cap | policy refused adds: memory **froze** at 300 | evict least important / least recently used | same. Never evicts an explicit "remember that…" |
| Always-on profile | 5 newest profile-kind items | LLM digest, refreshed lazily. **Still showed forgotten facts** | LLM digest **+ ranked fallback**. Invalidated by a version counter (§3.2), so a forgotten note can't come back. Lines are policy-checked and checked for **groundedness** |
| Read cost per turn | `enabled()` + `live()` read **every row and every embedding** (JSON) | Qdrant query | **one small state query**. Rows are re-read only when the learner's `version` changes (works across replicas). Scoring is one mat-vec over a cached, normalised matrix |
| Relevance | cosine **or** lexical; floor 0.35 | cosine; keyword only if Qdrant is down | **hybrid** `max(dense, lexical)`, each with its own floor, then `0.6·rel + 0.2·recency + 0.2·importance`, a diversity pass and a character budget |
| Importance | none (confidence did both jobs) | separate `importance` | separate `importance` (cost of forgetting) and `confidence` (chance it's true) |
| Embedder swap | silently compared vectors from different models | n/a | `embedding_model` stored with each vector. Mismatched vectors are never compared, and maintenance **re-embeds** them |
| Retention | expired rows hidden but never deleted; history kept forever | TTL loop | lazy periodic `maintain()`: prune history and expired rows after `retention_days`, re-embed stale vectors |
| Learner control | list / forget / forget all / off | list / remember / forget | + **remember** (`POST`), **edit** (`PATCH`), digest shown, **recall preview** |
| Minors-first policy | allowlist + PII denylist + vague-reference veto | none | kept and applied **twice**: candidates are checked before any second model sees them, and merged text is checked again. Digest lines are checked too |

**Calibration bug found along the way.** With `BAAI/bge-small-en-v1.5`, unrelated
memory/query pairs score **0.40–0.63** and related pairs **0.66–0.85**. v1's floor of 0.35
let everything through, so its "relevant" notes were top-k noise. The floor is now
`recall.dense_min: 0.65`. `test_the_configured_dense_floor_separates_related_from_unrelated`
(`RUN_INTEGRATION=1`) fails if someone swaps the embedder without recalibrating.

## 2. Write path (always off the request path)

```
student's message (+ previous exchange, as context only)
  │  MemoryJobQueue (threadpool | inline)
  ▼
extract   fast model → Candidate{kind, content, confidence, importance, explicit}
  ▼
policy    denylist / vague / confidence / kind — refused content never reaches another model
  ▼
neighbours   one batched embed; per candidate, top-k live items by hybrid relevance
  ▼
reconcile    add | update(target, merged) | retire(target) | no-op
             no neighbours → all adds, no model call · bad output → retry → rules
             explicit memories are rewritten/retired only by explicit candidates
  ▼
policy    again: merged sentences are new text
  ▼
apply     ONE unit of work per learner (threading lock | pg_advisory_xact_lock):
          re-validate against the live set → retire, update, add (dedupe, evict at cap)
          → bump version (and removal_version if anything was removed or rewritten)
  ▼
upkeep    digest refresh if stale · maintain() at most every N hours
```

Lessons commit **mastery** (code only, FSRS-lite) and an **episode** note through the
same `apply`, so the cap and versioning apply to them too.

## 3. Read path (per turn)

### 3.1 Two layers

- **Profile** (always on): who the learner is. This is the digest when a valid one exists,
  plus profile-kind notes added since it was built. Otherwise it's a ranked slice: explicit
  requests first, then by importance and recency.
- **Relevant**: notes relevant to the standalone question. With a digest, only
  `situational_kinds` (learning needs, episodes) are searched, and anything the profile
  already says is dropped.

The synthesizer shows them as separate blocks labelled *"use only to personalise — never
as facts, never cite"*. Learn mode gets the same notes as a flat list.

### 3.2 Versions, caching and digest validity

`memory_settings.version` goes up on every item change. `removal_version` goes up on every
change that **removes or rewrites** something (forget, retire, supersede, evict).

- **Cache**: `SnapshotCache[owner] = (version, items, normalised matrix)`. Each turn reads
  `state()`, which is one row joined with the digest. Rows are fetched only if the version
  moved, in any replica.
- **Digest**: shown only if `built_version ≥ removal_version`. A digest built before a
  forget may mention the forgotten fact, so it is hidden immediately and rebuilt in the
  background. If a removal lands *while* the digest is being built, that digest is never
  shown. This holds by construction, without coordination.
- Adds don't invalidate the digest (it's incomplete, not wrong). Newer profile notes are
  appended until the next rebuild.

### 3.3 Digest validation

The model output is split into bullet lines. A line is kept only if it passes the
sensitive-content policy **and** shares at least a third of its meaning words with some
source memory. If fewer than half the lines survive, the whole digest is rejected (the
model went off-task) and the ranked profile is used.

## 4. Invariants (tested)

`tests/test_memory_v2.py::test_memory_invariants_hold_under_random_use` runs random
ingest / forget / recall sequences against a reconciler that randomly updates and retires
its neighbours, and asserts after every step:

1. live items ≤ the cap;
2. no two live items state the same fact;
3. a forgotten note never comes back through recall, including through the digest;
4. a digest is shown only while it is valid.

The Postgres contract (`TEST_DATABASE_URL`) runs the same store tests, plus a 6-thread race
that must end with exactly one row and `version == 1`.

## 5. Configuration (`chat.memory.long_term`)

| Key | Default | |
|---|---|---|
| `extractor` | `llm` | `llm` \| `none` |
| `reconciler` | `llm` | `llm` \| `rules` |
| `profile` | `llm` | `llm` (digest) \| `ranked` |
| `recall.{top_k, dense_min, lexical_min, w_*, half_life_days, diversity, budget_chars}` | 3, 0.65, 0.25, .6/.2/.2, 30, 0.9, 900 | ranking |
| `profile_refresh_writes` / `profile_max_age_days` | 3 / 7 | when to rebuild the digest |
| `neighbours_k` / `neighbour_min` / `duplicate_similarity` | 5 / 0.3 / 0.92 | reconcile neighbourhood; rules-mode duplicate threshold |
| `max_items_per_user` | 300 | eviction cap |
| `retention_days` / `maintenance_interval_hours` / `episode_ttl_days` | 30 / 6 / 180 | hygiene |

**Embedder swap**: change `embedding.model`, recalibrate `recall.dense_min` (run the
integration test), and let `maintain()` re-embed. Lexical recall covers the gap meanwhile.

## 6. API (`/api/v1/me`)

`GET /memories` (items + `profile` digest) · `POST /memories` `{content, kind}` → 201, or
422 with the reason · `PATCH /memories/{id}` · `DELETE /memories/{id}` (hard, including the
history chain) · `DELETE /memories` (items, digest, mastery) · `GET /memories/recall?q=`
(exactly what a question would recall) · `PUT /memory-settings` · `GET /learning`.

## 7. Operations

- Migrate: `lessonforge-db migrate` (0004 is additive. It backfills `ended_at` for
  superseded rows and rebuilds the live index).
- Logs: `memory.apply owner=… add= update= retire= evict= skip=`,
  `memory.maintenance pruned= reembedded=`, `memory digest rejected …`.
- No memory component can fail a turn. Recall errors return an empty context, and write
  errors are logged inside the job.

## 8. Next

- Postgres outbox job queue for strict cross-replica ordering of summary folds (memory
  writes are already replica-safe).
- pgvector / Qdrant `MemoryStore` if per-learner caps ever grow past ~10k items. The
  port and the snapshot contract stay the same.
- Quiz results → mastery + learning-need candidates through `ingest`.
- A "memory updated" indicator in the chat (the apply result is already structured).
- An eval set of conversations with expected memory states, run against the live model.
