# AI Lesson Plan Creator — Software Requirements Document

> **Not "AI that writes lesson plans." AI that thinks like an experienced teacher.**

| | |
|---|---|
| **Version** | 0.9 (Draft — For Review) |
| **Curriculum** | Nepal · CDC / NEB |
| **Target** | Teacher-first, school-ready |

---

## § 01 — Vision & Positioning

Teachers lose hours turning a one-line topic into slides, worksheets, and a quiz. Existing AI planners produce those artifacts fast — but **generic**, because they treat the lesson plan as the final source of truth and let the model invent every detail. This product wins on the opposite bet: it invests in **pedagogical reasoning before generation**, so every artifact carries the judgement of a good teacher.

The competitive moat is not the export buttons — those are commodities. It is the layer that answers the questions a 20-year veteran asks automatically:

- What do students already know?
- What do they usually get wrong (misconceptions) at this grade?
- What hook earns attention *before* the definition?
- Which activity actually hits the objective?
- How do I stretch this to 60 minutes or compress it to 30?

The product's job is to make those answers **structural and repeatable** — grounded in Nepal's CDC/NEB curriculum and in the teacher's own past work.

> **Grounding artifact.** This SRD is written against a real teacher-authored reference: a science lesson on *Components of Environment — Biotic & Abiotic*, built on the **5E model** with bilingual phase labels (संलग्न गराउनु, अन्वेषण, व्याख्या गर्नु…) and hyper-local examples — paddy field, goat, mushroom, river. That document defines the shape of a "good" output and is treated as the canonical v1 template.

---

## § 02 — The Anti-Generic Doctrine

Generic output is a **structural** failure, not a prompt-wording failure. It comes from single-shot generation off a thin brief. We defeat it structurally: nothing the user types is the source of truth — the system **reasons its way to a rich intermediate object first**, and every artifact compiles from that object.

**✕ Generic (rejected):**
> "What is Photosynthesis? — Definition: the process by which plants make food…"
>
> Definition-first. No curiosity, no misconception, no activity. Fails validation.

**✓ Teacher-grade (required):**
> "You're stranded on an island with only plants. How do they make their own food with no shop?"
>
> Hook → predict → explore → explain. Engagement *before* the definition. Mirrors the reference lesson's "What did you see on your way to school today?"

### Guiding principles

1. **Reason before you render** — a validated Lesson Design Document is built and critiqued before a single slide exists.
2. **Consistency is structural, not hoped-for** — slides, worksheet, and quiz all derive from one object, so they cannot drift from the objectives.
3. **Ground, don't hallucinate** — specificity comes from retrieved curriculum, misconceptions, and exemplars, not model priors.
4. **Generate activities, not information** — every lesson must contain active learning; a bullet-list-only lesson is a defect.
5. **Local by default** — examples are drawn from the student's world; Nepal context is a first-class input, not an afterthought.
6. **Everything swappable** — LLM, vector store, pedagogical framework, and each export format sit behind interfaces — one config change to swap.

---

## § 03 — Scope & Users

Ship a **deep core with few outputs**, not many shallow ones. The expensive, defensible work is the reasoning layer; once it exists, new export formats are cheap plugins.

### 3.1 Release scope

**✓ v1 — In**
- Input: topic idea, CDC standard/topic code, or paste an existing plan
- Lesson Design Document (the reasoning core)
- 5E lesson plan → **DOCX + PDF**
- Slides → **PPTX**
- One **worksheet**
- One **quiz** (MCQ · True/False · Short answer)
- Objectives + homework (in the plan)
- Timing variants: 30 / 45 / 60 min
- Bilingual EN body + Nepali pedagogical terms
- Human edit before export

**◑ v1.5 — Next**
- Teacher personal corpus (learns their style)
- School / department shared corpus
- Student handouts, rubrics
- Full Nepali translation output
- Differentiation packs (struggling / advanced)
- Alternative frameworks (gradual-release, inquiry)

**○ Out (for now)**
- LMS integration / grading
- Student-facing accounts
- Video / interactive media generation
- Mobile-native app
- Real-time co-authoring

### 3.2 Personas & primary stories

- **Persona · Primary — Deepa, secondary science teacher.** Teaches 5 sections, limited prep time, mixes Nepali and English. Wants materials that feel like *hers* and use local examples her students recognize.
- **Persona · Secondary — Head of Department.** Wants consistent quality across teachers and alignment to CDC standards — without removing teachers' freedom to personalize.

| ID | User story |
|----|------------|
| US-1 | As a teacher, I enter a topic + grade + duration and receive a complete, editable 5E lesson plan grounded in CDC. |
| US-2 | As a teacher, I one-click export the plan, slides, worksheet, and quiz — all consistent with the same objectives. |
| US-3 | As a teacher, I paste my rough existing plan and the tool enriches it (adds hook, misconceptions, activities) rather than replacing it. |
| US-4 | As a teacher, I switch the same lesson between 30/45/60 minutes without regenerating from scratch. |
| US-5 | As a HoD, I set a shared style/standards profile the whole department inherits *(v1.5)*. |

---

## § 04 — The Lesson Design Document (LDD)

The LDD is the heart of the system — a **structured, validated, framework-agnostic object** that holds every decision an expert teacher makes before producing materials. It is the single source of truth; all exports are pure functions of it.

Think of the system as a compiler: the input is "source," the LDD is the typed intermediate representation, and each export is a code-generation target. Because the LDD schema *mandates* pedagogical structure, we can **validate that the thinking happened** — a lesson with no hook, no misconception, or an assessment that doesn't map to an objective simply fails.

The schema below is derived directly from the reference lesson's real structure, generalized so 5E is one pluggable framework among several.

```python
class LessonDesignDocument(BaseModel):
    # ── identity & framing ──
    topic: str
    grade: int
    subject: str
    duration_min: Literal[30, 45, 60]
    language: Literal["en", "ne", "en-ne"]      # bilingual mode
    curriculum_ref: CurriculumRef               # CDC/NEB standard, grounded
    framework: Literal["5E", "gradual_release", "inquiry"]

    # ── the teacher's reasoning (why output isn't generic) ──
    objectives: list[Objective]          # Bloom-tagged, measurable, id'd
    prior_knowledge: list[str]           # prerequisites assumed
    misconceptions: list[Misconception]  # RETRIEVED, grade-specific
    engagement_hook: Hook                # scenario/question — never a definition
    local_context: list[str]             # paddy field, goat, river…

    # ── the sequence (framework-shaped phases) ──
    phases: list[Phase]                  # Engage→Explore→Explain→Expand→Evaluate
    materials: list[str]
    differentiation: Differentiation     # struggling / on-level / advanced

    # ── assessment, mapped 1:1 to objectives ──
    formative_checks: list[Question]
    homework: Homework
    timing_variants: dict[int, list[Phase]]  # same lesson, 30/45/60

    quality: QualityReport               # critic scores + provenance


class Phase(BaseModel):
    name_en: str
    name_ne: str                         # "संलग्न गराउनु"
    teacher_activities: list[str]
    student_activities: list[str]
    minutes: int
    objective_ids: list[str]             # which objective this serves
```

> **Design rule · assessment alignment.** Every `Question` in `formative_checks` must reference at least one `objective_id`, and every objective must be touched by at least one phase *and* one check. This bidirectional mapping is validated — it is the mechanism that keeps slides, worksheet, and quiz provably on-objective.

### 4.1 Reference mapping — how `lp1` becomes an LDD

| Reference section | LDD field | Reasoning added by the system |
|-------------------|-----------|-------------------------------|
| Topic + Learning Objectives | `topic`, `objectives[]` | Bloom tags, measurability check, CDC alignment |
| Previous Knowledge | `prior_knowledge[]` | Retrieved prerequisites for grade |
| *(implicit)* | `misconceptions[]` | **New:** "clouds move but aren't living," retrieved |
| Engage: "What did you see…" | `engagement_hook` | Enforced as scenario, not definition |
| 5E phase table | `phases[]` | Nepali labels, per-phase objective mapping, timing |
| rice plant · goat · mushroom | `local_context[]` | Retrieved local exemplars, reused across all outputs |
| Evaluation Questions + Homework | `formative_checks[]`, `homework` | Mapped 1:1 to objectives; quiz auto-derives |

---

## § 05 — System Architecture

A typed pipeline of loosely-coupled stages. Each stage is `Stage[In, Out]` — independently testable, mockable, reorderable. The LLM, retriever, and renderers are injected dependencies, never hardcoded.

```
1. Intake & Normalization      input → NormalizedBrief
   Parse idea / CDC code / pasted plan. Extract grade, subject, duration,
   language, standard. Cheap fast model.
        │
2. Pedagogical Enrichment      Brief + retrieval → LDD (draft)
   The "teacher brain." Retrieves standards, misconceptions, prior knowledge,
   local examples, teacher prefs; builds the structured LDD. Strong model.
        │
3. Critique & Revise           LDD → LDD (validated)
   Scores the draft against the anti-generic rubric, rewrites weak sections,
   runs schema + alignment validators. Loops until it passes or budget hits.
        │
4. Material Generation         LDD → ContentModels
   Per-artifact structured generation (slides, worksheet, quiz models) —
   all reading the SAME LDD.
        │
5. Render & Assemble           ContentModels → files
   Template-driven renderers emit DOCX / PPTX / PDF. Deterministic, no LLM.
   One-click export bundle.
```

> **Loose coupling — the seams that matter.** `LLMClient`, `Retriever`, `PromptRegistry`, and `Renderer` are all interfaces resolved by config + dependency injection. Stages compose into a `Pipeline` you can reorder or stub. Swapping a model, vector DB, prompt version, or adding a new export format touches **zero core logic**.

---

## § 06 — Knowledge & RAG

The differentiator is retrieving **pedagogical content knowledge** — *how* to teach this to *these* students — not just facts. Five independent collections, each with metadata filters (grade, subject, standard, language). Retrieval feeds the **enrichment stage**, not only final generation. Hybrid dense + keyword (BM25) with grade/subject filtering.

| # | Collection | Powers | Phase |
|---|-----------|--------|-------|
| 1 | **CDC / NEB Standards Corpus** | Correct alignment, objective phrasing | v1 (priority) |
| 2 | **Pedagogical Knowledge** (misconceptions, strategies, 5E patterns) | The hook, misconceptions, activities — *the moat* | v1 |
| 3 | **Teacher Personal Corpus** (past lessons, uploads) | Learns voice & preferences | v1.5 |
| 4 | **School / Dept Shared Corpus** | Standardization across classes | v1.5 |
| 5 | **Exemplar Lessons** (hand-picked, like the reference) | Few-shot grounding, raises quality floor | v1 |
| + | **Local Context Bank** (paddy field, goat, monsoon, terraced hills) | Recognizable, non-imported hooks | v1 |

> **Why RAG here, specifically.** Ask an ungrounded model for a science hook and you get an American grocery-store analogy. Ground it in the Local Context Bank + Nepal exemplars and you get "What did you see on your way to school today?" — the reference lesson's actual opener. That gap *is* the product.

---

## § 07 — LLM Abstraction & Configuration

Hard requirement: **changing the LLM is one config change.** Achieved with a thin gateway interface plus *per-stage* model selection — a cheap fast model for extraction, a strong reasoning model for enrichment/critique. Prompts are versioned templates in a registry, never hardcoded, so they can be A/B-tested and evaluated.

```yaml
# config/models.yaml
defaults:
  provider: anthropic
  reasoning_model: claude-opus-5       # enrichment + critique
  fast_model:      claude-haiku-4-5    # intake, extraction

stages:
  intake:      { model: fast_model,      temperature: 0.0 }
  enrichment:  { model: reasoning_model, temperature: 0.7 }
  critique:    { model: reasoning_model, temperature: 0.2 }
  generation:  { model: reasoning_model, temperature: 0.5 }

# Swap the whole system to another provider by editing 'provider'
# and the two model ids. Zero code changes; contract tests guard the seam.
```

All model calls go through `LLMClient.complete(prompt, schema, cfg)`. Structured outputs are requested against a JSON schema and parsed into Pydantic — so "bad output" surfaces as a typed validation error, not a silent quality drop.

> **Provider default.** Reasoning stages default to the latest Claude (Opus / Sonnet 5) for pedagogical judgement quality — but that is a config value behind the `LLMClient` interface, never a code dependency. A provider adapter implements the interface; contract tests keep every adapter honest.

---

## § 08 — Output Generation & Layout

Renderers are **template-driven and deterministic** — the LLM never invents layout. Predefined DOCX/PPTX themes are populated from the artifact content models. This keeps output professional, on-brand, and testable via golden files.

| Format | Artifact | Notes |
|--------|----------|-------|
| DOCX / PDF | **Lesson Plan** | Reproduces the reference layout: header block, objectives, previous knowledge, methods, materials, then the 5E table (Phase · Teacher · Student · Time), closure, evaluation, homework. `python-docx` → PDF. |
| PPTX | **Slide Deck** | Hook-first deck: engage → predict → explore → explain → check. One idea per slide, speaker notes from teacher activities. `python-pptx`. |
| DOCX | **Worksheet** | Sort/classify + apply tasks built from `local_context` and phases. Answer key generated from the same LDD. |
| DOCX / JSON | **Quiz** | MCQ · True/False · Short answer, each tagged to an objective. Distractors seeded from retrieved misconceptions. |

> **Nepal rendering constraint · verify early.** Bilingual output means **Devanagari must render correctly** in DOCX and PPTX (phase labels like व्याख्या गर्नु). This requires embedding/declaring a Unicode Devanagari font (e.g. Noto Devanagari) in the templates — a known failure point. Treat "Devanagari renders in exported DOCX + PPTX + PDF" as an explicit v1 acceptance test, not an assumption.

---

## § 09 — The Anti-Generic Quality Engine

Three structural defenses turn "please be specific" from a hope into a guarantee:

1. **Required structure (validators)** — schema mandates a scenario hook, ≥1 grade-specific misconception, ≥1 active-learning phase, and full objective↔assessment mapping. Missing any → validation failure, not a weak lesson.
2. **Critique → revise loop** — a critic scores the LDD on a rubric (engagement, alignment, misconception coverage, specificity, local relevance) and rewrites low-scoring sections. Loops to threshold or budget.
3. **Specificity gate** — heuristic + judge checks reject definition-only slides and demand concrete examples/questions. "What is X? Definition…" is a lint error.

Plus **grounding citations**: retrieved facts carry source tags into the LDD (`QualityReport`), improving specificity and letting teachers trust/verify alignment.

---

## § 10 — Testing & Evaluation

"Well tested" for a stochastic system means **deterministic seams + an eval harness.**

| Layer | Technique | Guards against |
|-------|-----------|----------------|
| Schema validation | JSON-schema / Pydantic on every LLM output | Malformed or incomplete reasoning |
| Unit tests | LLM mocked; test parsing, routing, mapping | Logic regressions (deterministic) |
| Golden files | Fixed LDD → byte-stable DOCX/PPTX | Renderer & layout regressions |
| Contract tests | Every `LLMClient`/`Retriever`/`Renderer` impl | Broken swappability |
| Eval harness | Rubric scoring (structural + LLM-judge) on a fixed lesson set, in CI | Silent quality drift on prompt/model change |
| Alignment assertions | Auto-check objective↔phase↔assessment coverage | Off-objective materials |

> **Eval as a first-class asset.** Curate a "golden set" of ~30 diverse CDC topics (incl. the reference lesson). Every prompt or model change is scored against it — quality becomes a measured number, not a vibe. This set is also the acceptance gate for swapping providers.

---

## § 11 — Data Model & Tenancy

You chose **teacher-first, school-ready** — so the data model is multi-tenant from day one even though the v1 UI is single-teacher. Every row is scoped by `org_id` (nullable → personal) and `owner_id`. Corpora are scoped the same way, so a shared department corpus in v1.5 is a new scope, not a migration.

- `User` → belongs to optional `Organization`; roles: `teacher`, `hod`, `admin`.
- `Lesson` → owns an `LDD` (versioned) + generated `Artifacts`; scoped by owner/org.
- `Corpus` → typed (`personal` / `shared` / `curriculum`); retrieval respects scope + permissions.
- `TeacherProfile` → stored preferences (style, language mix, default framework) injected into every LDD build.

---

## § 12 — Non-Functional Requirements

| Area | Requirement |
|------|-------------|
| **Performance** | Full lesson + 4 artifacts in target < 60s; stream progress per pipeline stage. |
| **Cost** | Cheap model on cheap stages; cache retrieval + intermediate LDDs; token budgets per request. |
| **Reliability** | Graceful degradation — if critique/retrieval fails, still return a valid (if less enriched) LDD. |
| **Context · Nepal** | Low-bandwidth aware: lightweight web UI, resumable exports, offline-friendly downloads. |
| **Privacy** | Tenant isolation — corpus & lesson data never cross `org` boundaries; teacher uploads private by default. |
| **Extensibility** | New output format = implement `Renderer`, register it. No core change. |

---

## § 13 — Technology Stack

| Layer | Choice | Why |
|-------|--------|-----|
| Core language | Python 3.12+ | Best DOCX/PPTX ecosystem; strong LLM/RAG tooling |
| API | FastAPI + Pydantic v2 | Typed contracts = the LDD schema is also the API schema |
| LLM gateway | Thin custom `LLMClient` + provider adapters | One-config swap; avoids lock-in to a heavy framework |
| Vector store | pgvector (start) → Qdrant (scale) | pgvector keeps infra to one DB early; interface allows swap |
| DOCX / PPTX | python-docx · python-pptx | Template-driven, deterministic, testable |
| PDF | DOCX→PDF (LibreOffice headless) or WeasyPrint | Fidelity to the DOCX template |
| Reasoning model | Claude Opus/Sonnet 5 (default, swappable) | Pedagogical judgement quality |
| Frontend | Lightweight web (later) | Low-bandwidth first; editing UI in v1.x |

---

## § 14 — Roadmap

| Milestone | Deliverable | Detail |
|-----------|-------------|--------|
| **M1** | Vertical slice: idea → LDD → 1 slide deck | Prove the compiler thesis end-to-end on the reference topic. LLM gateway + LDD schema + one renderer. RAG stubbed. |
| **M2** | Grounding: CDC + pedagogical + exemplar RAG | Ingest Nepal curriculum + misconceptions + local context bank. Enrichment uses real retrieval. Measurable jump on golden set. |
| **M3** | Full v1 export bundle: plan · slides · worksheet · quiz | All four renderers, Devanagari verified, timing variants, one-click export. Critique loop + validators on. |
| **M4** | Edit & personalize: UI + teacher profile | Web editing before export; stored preferences. Eval harness in CI as release gate. |
| **M5** | School-ready: personal + shared corpus | v1.5: teacher personal corpus ("learns from past lessons"), department shared corpus, HoD standardization. |

---

## § 15 — Risks & Open Questions

### Risks

| Area | Risk | Mitigation |
|------|------|-----------|
| Content | CDC corpus may not be machine-ingestible (PDFs, OCR) | De-risk sourcing in M2 planning; drives timeline |
| Rendering | Devanagari fidelity in PPTX/PDF is finicky | Spike in M1, not M3 |
| Quality | LLM-as-judge can be noisy | Anchor with structural checks + periodic human review |
| Cost | Critique-loop token spend multiplies | Cap iterations; measure quality-per-token |

### Decisions still needed

- Which **grades & subjects** for v1 focus? (Reference is secondary science — start there?)
- Source & licensing of the **CDC standards corpus** and any misconception datasets.
- Default **language mode**: EN body + NE terms (like the reference), or full bilingual?
- Hosting & data-residency expectations for Nepal schools.
- Is a **"paste existing plan → enrich"** flow a v1 headline feature or v1.5?

---

*AI Lesson Plan Creator · SRD v0.9 · 2026-08-06 · "AI that thinks like an experienced teacher."*
