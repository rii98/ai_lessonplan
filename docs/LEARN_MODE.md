# Learn mode & the memory layer — design proposal

Status: **implemented** (2026-11-03) on `feat/edboox-integration`, not yet committed. §14 lists
what was built and where the build differs from this proposal. Builds on
[CHAT_SKILLS.md](CHAT_SKILLS.md) (router + skills + artifacts) and the chat pipeline in
`services/chat/`.

Learn mode is the chat **teaching** a concept instead of **answering** it. It builds the
student's understanding step by step with questions, hints, worked steps and inline
interactive checks (MCQs, ordering, categorising, predict-then-reveal…). The second half of
this doc covers the **memory layer**, which normal chat and learn mode both use.

---

## 0. TL;DR — the decisions

| # | Decision | Why |
|---|----------|-----|
| D1 | Learn mode is a **stateful session** (`LearningSession`), not a one-shot skill. A `learn` skill only *starts* one; after that, a `LearnTurnHandler` handles the conversation's turns until the session ends. | Skills are one-shot (`run()` → artifact). Tutoring runs over many turns and needs a goal, a position in the plan and per-step state. |
| D2 | **Plan first, then tutor.** At session start a `LessonPlanner` retrieves **once**, broadly, and compiles a grounded `LearningPlan`: objectives → expectations, misconceptions, a hidden answer key, check items, and **pinned evidence**. | This is AutoTutor's expectation/misconception structure plus Khanmigo's and Bastani's finding that the model is more accurate when it holds the solution. The plan is what stops drift. |
| D3 | **RAG is policy-driven, not per-turn.** Most turns use no retrieval: the current objective's pinned evidence is reused. Retrieval runs only on (a) moving to a new objective, (b) an in-scope question the pins can't cover, (c) a remediation that needs a different explanation. Queries are **anchored to the objective**, never the student's raw message alone. | This answers the drift concern directly. A student's tangent can't pull in new context that takes over the session. |
| D4 | **The next teaching move is chosen by code, the LLM only phrases it.** Each turn: *trace* the student's state (fast LLM, structured) → update the *learner model* (code) → *policy* picks a `TutorMove` (deterministic state machine) → *renderer* (LLM) phrases that one move. | StratL found a hand-built transition graph followed the strategy better than letting an LLM choose. Bridge found telling the model the error type and strategy improved responses by 75%. MathDial found an unguided ChatGPT tutor revealed the answer about 66% of the time. |
| D5 | **Interactive checks are typed widgets graded on the server**, built from the quiz `Question` IR and `RuleGrader` that already exist. A widget answer is a structured turn that **skips the LLM tracer**. | Reuses the quiz foundation. Grading is deterministic and the answer is never sent to the client. Widget turns are cheaper and more accurate. |
| D6 | **Memory has four layers with different stores and rules**: working context (per turn) · conversation (window + structured rolling summary) · **session state (structured, never summarised)** · long-term user memory (profile facts, mastery, episodes). | Tutoring state kept in an LLM summary gets lost. Mastery must be computed by code, because LLM-based knowledge tracing is inconsistent. Users are minors, so memory needs strict governance. |
| D7 | **Mastery is updated only by code** (BKT-style, with forgetting and review scheduling). The LLM never writes a mastery number. | Research shows LLMs make inconsistent mastery updates, sometimes in the wrong direction. BKT/FSRS are proven and cheap. |
| D8 | Each new piece is a **port + registry + config block**, like the rest of LessonForge. `ChatPipeline.stream` is split into `TurnHandler` strategies (QA / Skill / Learn). | Follows the user's rule: loosely coupled, swappable with one config change, testable with fakes. |

> **Naming:** `ChatMessageRequest.mode` already means *scoped/broad retrieval*
> (`FilterMode`). Learn mode must **not** reuse `mode`. This doc uses *learning session*
> and `learn` throughout.

---

## 1. What the research says (and what we take from it)

| Source | Finding | What we adopt |
|--------|---------|---------------|
| **AutoTutor** (Graesser et al.) — *Expectation & Misconception Tailored* dialogue | Each problem has a list of **expectations** (ideas a good answer contains) and **misconceptions**. The tutor checks the student's turns against them and uses pumps → hints → prompts → assertions until every expectation is covered. One of the most evaluated conversational tutors. | `Objective.expectations` / `Objective.misconceptions`, a coverage score per expectation, and the **hint ladder** (pump → hint → prompt → bottom-out). |
| **StratL** (Puech et al., Findings ACL 2025) | Write the teaching strategy as a **transition graph**. Each turn: student-state tracer → intent selection → *intent-specific prompt addition*. The hand-built graph followed the strategy better than LLM selection. Code: github (linked from arXiv 2410.03781). | `StudentStateTracer` → `PedagogicalPolicy` (graph in code/config) → `TutorRenderer` with one move per turn. |
| **Bridge** (Wang, Demszky et al., NAACL 2024) | Experts first decide **error type → strategy → intention**, then reply. Giving the model these decisions improved responses by **75%**. | The tracer returns an error type. The policy returns a strategy and intention. The renderer gets both explicitly. |
| **MathDial** (Macina et al., EMNLP 2023) | An unguided ChatGPT tutor **revealed the solution in about 66%** of cases and gave wrong feedback in about 59%. | A hidden answer key plus an `AnswerLeakGuard`. A separate tracer so the tutor doesn't "solve" while it phrases. |
| **Bastani et al.** (PNAS 2025) — about 1,000 high-school maths students | Plain GPT-4 practice → **17% worse** on the later exam (students leaned on it). A guarded tutor (hints, not answers; given teacher solutions) removed the harm. | Hints by default; teacher-quality solutions in the plan (from our curriculum RAG); transfer checks after any "just tell me". |
| **Kestin et al.** (Sci. Reports 2025) — Harvard physics RCT | An AI tutor built on **cognitive-load management, small steps, active thinking, growth mindset** roughly doubled learning gains compared with in-class active learning. | Small objectives (3–6 per session), one idea per turn, attempt-before-reveal, encouraging tone. |
| **LearnLM** (Google, arXiv 2412.16429) and Gemini Guided Learning | Five rubric principles: **inspire active learning, manage cognitive load, adapt to the learner, stimulate curiosity, deepen metacognition**. Pedagogy is set through *system-level instructions* per turn. | These five are the **rubric** for our eval harness (§8) and the base persona prompt. |
| **ChatGPT Study Mode / Claude Learning mode** | Built from prompting alone: calibrating questions first, scaffolded reveal, periodic knowledge checks, "how would you approach this?". OpenAI accepts "some inconsistency" because it is prompt-only. | We go further and make the strategy **structural** (planner + policy), so behaviour stays consistent across models, including Gemma. |
| **Khanmigo** (Khan Academy blog) | Giving the model human-written steps/solutions **improved accuracy**. Long prompts get partly ignored, so split them into chained, single-purpose prompts. Removing the calculator tool doubled maths errors. | Small, single-purpose prompts per stage. Grounded answer key. A deterministic numeric checker. |
| **Knowledge tracing** (BKT; TutorLLM; 2025 LLM-as-tracer studies) | Hybrid designs work best: BKT for mastery, FSRS or a forgetting curve for review, the LLM for phrasing. LLM-only tracers were inconsistent. | `LearnerModel` = BKT plus a forgetting/review schedule, written in code (D7). |
| **Mem0 / Letta (MemGPT) / Zep** | Mem0: extract facts → **ADD/UPDATE/DELETE/NOOP** against similar existing memories. Letta: memory managed by the agent (costly, needs tool calls). Zep: facts with time ranges (needs a graph DB). Production systems usually combine approaches. | Mem0-style extraction pipeline. Zep-style `valid_from`/`superseded_by` columns **in Postgres** (no graph DB). We don't adopt agent-managed memory (too expensive, hard to govern for minors). |

**What we change from these sources:**
1. StratL's graph is per-strategy and hand-built. We make the graph **data** (a config/YAML
   policy) so a pedagogy lead can tune it without code, and a second strategy (e.g.
   productive failure, worked-example-first) is one new file.
2. AutoTutor's expectations were written by hand per problem. We **generate them from the
   curriculum corpus at plan time** and validate them with Pydantic guardrails (the same idea
   as the LDD validators).
3. Study Mode style products are prompt-only. We keep the prompt for *voice* and use
   **structure for behaviour**, so a weaker or cheaper model still tutors correctly.

---

## 2. Where it sits in the current system

```
                        ┌──────────── ChatPipeline (orchestrator only) ────────────┐
user turn ─► load conv ─┤ active LearningSession?                                  │
                        │   yes ─► LearnTurnHandler ───────────────┐               │
                        │   no  ─► IntentRouter ─► qa  ─► QATurnHandler (today)   │
                        │                       └► skill ─► SkillTurnHandler       │
                        │                            └ "learn" skill = start session│
                        └───────────────────────────────────────────┼──────────────┘
                                                                    ▼
             ┌────────────────────── LearnTurnHandler ─────────────────────────────┐
             │ 1 trace student turn  (StudentStateTracer; skipped for widget turns)│
             │ 2 update learner model (LearnerModel — code)                        │
             │ 3 pick move           (PedagogicalPolicy — state machine)           │
             │ 4 maybe retrieve      (RetrievalPolicy — usually NO)                │
             │ 5 phrase move         (TutorRenderer — LLM, streamed)               │
             │ 6 guard               (AnswerLeakGuard)                             │
             │ 7 emit widget/state   (WidgetFactory → learn_widget, learn_state)   │
             │ 8 persist session + message; enqueue memory jobs                    │
             └─────────────────────────────────────────────────────────────────────┘
```

### 2.1 Entry and exit

- **Explicit entry (primary):** a *Learn* toggle in the composer (like Study Mode), or the
  **"Teach me this"** chip under a QA answer. Calls `POST /conversations/{id}/learning-sessions {topic?}`.
- **Routed entry:** the `learn` skill joins the router catalogue like any other skill.
  Examples: "teach me photosynthesis", "I don't get fractions, help me understand step by
  step". Counter-examples: "what is photosynthesis?", "quiz me". When confidence is low the
  router **offers** learn mode (a chip) instead of switching. Students should choose the
  slower path themselves; we don't force it on them.
- **While a session is active**, the router still runs, but on a **small in-session
  catalogue**: `exit` ("stop", "just give me the answer and end"), `pause`, `switch_topic`,
  `quiz` (handled *inside* the session as a check), `skip` ("I know this"). Everything else
  goes to the tutor.
- **Exit:** completed (all objectives done), ended by the user, paused (resume later, even
  the next day), or abandoned (idle TTL → paused).

### 2.2 Why not "just a skill with a long prompt"

A single system prompt ("be Socratic, don't give answers") is what Study Mode does, and
OpenAI says it is inconsistent. It also leaves *every* decision to the model: when to hint,
when to reveal, when to move on, when to retrieve. That is exactly where drift and answer
leaks come from (MathDial: 66%). Splitting **decide** (code) from **phrase** (LLM) makes
each part testable without an LLM, and keeps behaviour stable across models (Gemma, Claude
or anything else behind `LLMClient`).

---

## 3. Domain model (pure Pydantic, `domain/learning.py`)

```python
class Expectation(BaseModel):          # an idea a complete answer must contain (AutoTutor)
    id: str
    statement: str                     # "Chlorophyll absorbs light energy"
    keywords: list[str] = []           # cheap coverage signal (stems, EN + NE)

class Misconception(BaseModel):
    id: str
    statement: str                     # "Plants get their food from the soil"
    diagnostic: str                    # a question that exposes it
    remedy: str                        # the corrective idea / contrast case

class CheckItem(BaseModel):            # pre-generated, validated interactive check
    id: str
    widget: WidgetKind                 # mcq | order | categorize | predict | fill | numeric | match | explain …
    question: Question                 # REUSES the quiz IR (answer stays server-side)
    targets: list[str]                 # expectation / misconception ids it measures

class Objective(BaseModel):            # one knowledge component; 3–6 per session
    id: str
    kc_ref: str | None                 # curriculum standard code (e.g. SEED-SC6-ENV-1) → mastery key
    title: str
    prerequisites: list[str] = []      # objective ids
    expectations: list[Expectation]    # ≥ 1
    misconceptions: list[Misconception] = []
    worked_example: str                # the hidden, grounded "teacher solution"
    hint_ladder: list[str]             # graded hints, least → most revealing
    checks: list[CheckItem]            # ≥ 1 (an objective is never "taught" without a check)
    evidence: list[PinnedChunk]        # pinned grounding: chunk ids + text + citation

class LearningPlan(BaseModel):
    goal: str                          # "Explain how plants make food"
    scope: PlanScope                   # grade, subject, chapter, language, filters actually used
    objectives: list[Objective]        # topologically ordered by prerequisites
    hook: str                          # curiosity opener (LearnLM: stimulate curiosity)
    version: int = 1                   # bumped on replan

class ObjectiveState(BaseModel):
    status: Literal["pending","active","mastered","skipped","struggling"]
    coverage: dict[str, float]         # expectation id → 0..1
    misconceptions_seen: list[str]
    hint_level: int = 0                # position on the ladder (fades back on success)
    early_evidence: bool = False       # covered while another objective was active (§5.1)
    attempts: int = 0
    p_mastery: float                   # session-local BKT estimate

class LearningSession(BaseModel):      # the aggregate (persisted as one row + jsonb)
    id: str; conversation_id: str; owner_id: str
    status: Literal["planning","active","paused","completed","ended"]
    plan: LearningPlan
    cursor: str                        # active objective id
    states: dict[str, ObjectiveState]
    phase: TutorPhase                  # state-machine node (see §5)
    parking_lot: list[str] = []        # off-topic questions deferred to the end
    tangent_budget: int = 2            # in-scope tangents allowed before steering back
    turn_count: int = 0
    created_at / updated_at / last_active_at
```

Guardrail validators (the same idea as the LDD validators): each objective has at least one
expectation and at least one check; every check targets an existing expectation or
misconception; prerequisites form a DAG; there is at least one pinned evidence chunk unless
`scope.ungrounded=True` (flagged in the UI).

---

## 4. RAG strategy: retrieve per objective, not per turn

The concern was that retrieving on every turn pulls the session off its goal. Retrieval
becomes a **policy** (`RetrievalPolicy` port) with four triggers. Its default answer is
*no*.

| Trigger | When | Query is built from | Collections | Result goes to |
|---------|------|--------------------|-------------|----------------|
| **T0 Plan** | session start / replan | goal + scope (multi-query, existing `QueryTransformer`) | curriculum, reference, pedagogical, exemplar, local_context | the plan; split across objectives as **pins** |
| **T1 Advance** | the cursor moves to an objective whose pins are thin (score < floor), or that was planned lazily | `objective.title + expectations` (**not** the chat) | reference, curriculum | that objective's pins (persisted) |
| **T2 Coverage gap** | the tracer says `question_in_scope` **and** the question isn't covered by the active pins (cheap check: max embedding/BM25 similarity < τ) | `objective.title + student question`, filtered to `plan.scope` | reference, curriculum | **this turn only** (ephemeral context); not pinned |
| **T3 Remediation** | the policy picks `reexplain` / `analogy` after ≥ 2 failed attempts | `misconception.statement` or `objective.title + "analogy / everyday example"` | pedagogical, local_context | this turn only |

Rules that stop drift:
1. **Anchor every query.** It always contains the active objective. A tangent ("do cacti
   do photosynthesis?") becomes "photosynthesis in plants — cacti?", so retrieval stays in
   the topic's neighbourhood.
2. **Fixed scope.** `plan.scope` (grade/subject/chapter filters) applies to T1–T3 with the
   existing lenient fallback. Only T0 can widen it.
3. **Ephemeral and pinned context are separate.** T2/T3 chunks are used for one turn and
   dropped. Only the plan and T1 change the session's grounding. One tangent can't
   permanently change what the tutor treats as the source material.
4. **Off-topic doesn't retrieve.** `question_off_topic` → acknowledge briefly, add it to the
   `parking_lot`, steer back. The parking lot is offered at the end ("You asked about X
   earlier, want to explore it?"). Students' curiosity is kept without losing the goal.
5. **Tangent budget.** In-scope tangents are welcome (curiosity), up to `tangent_budget` per
   objective. After that the policy returns to the active objective.
6. **Topic switch = replan, explicitly.** "Actually, teach me respiration" → confirm → the
   current session is *paused* (resumable) and a new session is planned. We never quietly
   change the goal of a running session.

Cost: T0 is one retrieval set per session. T1 is at most once per objective, and only when
needed. T2/T3 are rare. For a 20-turn session that is about 2–5 retrievals instead of 20.

**Lazy planning (latency):** T0 plans the whole outline (titles, order, kc_refs) plus
**full detail for objective 1 only**. Objectives 2..n are filled in with T1 in the
background while the student works on objective 1. The first tutor turn arrives in
roughly the time of one normal answer.

---

## 5. The tutoring loop

### 5.1 Student-state tracer (`StudentStateTracer` port)

Input: the student's message, the active objective (expectations, misconceptions), a
**compact list of the key ideas of every other objective** (one line each), the last tutor
move and the last 2–4 turns. Output (strict JSON, using the existing lenient parser and
few-shot; see the Gemma `format` quirk):

```json
{"act": "answer|question_in_scope|question_off_topic|help_request|skip_request|stop_request|switch_topic|chitchat",
 "correctness": "correct|partial|incorrect|n/a",
 "covered": {"O1.e1": 0.9, "O3.e1": 0.7},
 "misconception": "O1.m1" | null,
 "affect": "neutral|confused|frustrated|bored|confident",
 "topic": "respiration" | null}
```

The tracer also recognises in-session commands (`skip_request` "I know this",
`stop_request`, `switch_topic`), so a learn-mode turn needs **one** classification call
instead of running the general intent router as well.

#### Early credit for later objectives (the "O3 answered during O1" case)

Students don't follow our order. While working on O1 a student may say something that
belongs to O3 ("the sun helps it make sugar"). The tracer scores coverage against **all**
objectives' expectations (ids are namespaced `O3.e1`), and the learner model records it:

1. **Credit is recorded, not acted on immediately.** `ObjectiveState.coverage` of O3 is
   updated and O3 gets `early_evidence=True`. The cursor stays on O1, because O3 may need
   O2 first (prerequisites).
2. **The tutor acknowledges it straight away.** The policy passes a `noticed_ahead` note to
   the renderer: *"You already know plants make sugar, nice! We'll come back to that."*
   The student feels heard and nothing is skipped.
3. **When the cursor reaches O3, it starts with a check, not teaching.** Pass → mastered
   in one turn. Fail → teach only the expectations that are still uncovered.
4. **If O3 doesn't depend on the current path**, the policy may offer to go there next
   ("Want to explore that now?"), the same as a curiosity pull-forward (§5.6).
5. **Talk alone never masters an objective.** Early credit raises `p_mastery`, but mastery
   still needs a passed check, so a lucky phrase can't skip real understanding.

Providers: `llm` (fast model, default) · `rules` (keywords + regex, offline/test seam) ·
`hybrid` (rules first, LLM when unsure). Coverage is **combined** with a deterministic
keyword/embedding score against `Expectation.keywords`, so one bad LLM call can't mark an
objective mastered.

**Widget turns skip the tracer completely.** The `RuleGrader` result maps directly to
correctness, coverage (via `CheckItem.targets`) and misconception (an MCQ distractor can be
tagged with the misconception it shows, which is a very strong signal).

### 5.2 Learner model (`LearnerModel` port, code only)

- Session-local BKT per objective: `p_mastery` updated from the evidence (widget = strong,
  tracer = weak/discounted). Parameters live in config.
- `mastered` when `p_mastery ≥ 0.85` **and** at least one interactive check was passed
  (dialogue alone is never enough).
- On session end, results go to the long-term `MasteryRecord` for each `kc_ref` (§7.3).
- At session start, the long-term mastery **seeds** the plan: likely-known objectives get a
  quick diagnostic check instead of full teaching ("I already know this" is checked, not
  assumed).

### 5.3 Pedagogical policy (`PedagogicalPolicy` port, a state machine)

Moves (`TutorMove`), from the AutoTutor, Bridge and MathDial taxonomies:

| Move | Purpose |
|------|---------|
| `hook` | curiosity opener / real-world puzzle |
| `diagnose` | find out what they already know (calibrate) |
| `elicit` | open question that targets an uncovered expectation |
| `pump` | "what else?" / "why do you think so?" |
| `hint(level)` | next rung on the ladder |
| `prompt` | fill-in-the-blank style nudge for one specific word/idea |
| `worked_step` | show ONE step of the worked example, then ask for the next |
| `check(widget)` | an interactive check from `objective.checks` |
| `confront(misconception)` | a contrast case / "what would happen if…" |
| `affirm_and_extend` | confirm a correct answer + deepen ("why does that work?") |
| `reexplain` / `analogy` | another explanation after repeated failure (may trigger T3) |
| `bottom_out` | give the answer **with reasoning**, then a transfer check |
| `reflect` | metacognition: "how would you explain this to a friend?" / confidence rating |
| `explain_chunk` | teach ONE small idea + example, then ask (novice path, ask/tell balance) |
| `challenge` | ⭐ a harder transfer question for fast learners |
| `choose_next` | let the student pick between independent next objectives |
| `recap_advance` | summarise the objective and move the cursor (offers a break) |
| `answer_tangent` / `park` | handle in-scope / off-topic questions |
| `wrap_up` | session summary, parking lot, schedule a review |

Phases (graph nodes): `hook → diagnose → teach(objective)* → consolidate → wrap_up`. Inside
`teach`, the sub-graph is roughly:

```
elicit ──correct──► affirm_and_extend ──all expectations covered──► check
  │                                                                  │
  ├─partial──► pump ─► (elicit next uncovered expectation)           ├─pass─► reflect? ─► recap_advance
  ├─incorrect + misconception──► confront ─► elicit                  └─fail─► hint ladder ↺
  ├─incorrect──► hint(level+1) … ─► worked_step … ─► bottom_out ─► transfer check
  └─help_request / wants_answer──► (attempts==0 ? prompt once : bottom_out) ─► transfer check
```

Policy rules (all config, not prompt text):
- **Productive struggle budget:** at most `max_hints` (default 3) failed rungs, then
  `worked_step`, then `bottom_out`. We don't keep a student stuck until they give up.
- **Frustration override:** `affect=frustrated` jumps two rungs and switches to an
  encouraging tone. `bored/confident` → skip to `check` (they may already know it).
- **"Just tell me":** respected after at most one nudge, never refused or lectured (tone).
  Always followed by a **transfer check** on a sibling problem. This is the Bastani lesson:
  the answer is fine, the crutch is the problem.
- **Fading:** `hint_level` goes down after successes, so support is removed as competence
  grows (fading is the core of scaffolding).
- **Interaction cadence:** at least one interactive widget every `k` tutor turns (default
  3). No more than two text-only turns in a row.
- **Spacing:** at the start of a resumed session, `check` the last mastered objective first
  (retrieval practice).

Providers: `graph` (default: the transition table loaded from
`config/pedagogy/<strategy>.yaml`) · `llm` (experimental, for comparison) · `scripted`
(tests). A new strategy (productive failure, worked-example-first for younger grades) is a
new YAML file, not new code.

### 5.4 Renderer (`TutorRenderer` port) and leak guard

The prompt is **small and single-purpose** (the Khanmigo lesson):

```
[persona]   friendly Nepali-curriculum tutor; grade-appropriate; student's language (en/ne)
[move]      ONE instruction for this move, e.g. hint level 2: "Point the student at where
            the plant's energy comes from without naming 'sunlight'. One or two sentences,
            end with a question."
[objective] title + uncovered expectations
[evidence]  pinned chunks [n] (+ ephemeral T2/T3 chunks)
[hidden]    worked_example / answer — "for your reasoning only; never state it unless the
            move is bottom_out"
[learner]   brief: attempts, misconception seen, affect
[history]   last 4 turns verbatim + session recap line
```

Output streams as normal `token` events. The move/phase/objective go into message `meta`
(for analytics and eval).

`AnswerLeakGuard` (port; `rules` default): before streaming starts for non-`bottom_out`
moves, the renderer's first tokens are buffered for a moment and the full text is checked
against the active check items' answers (normalised string, numeric value, MCQ letter) and
the worked example's final result. On a hit it **regenerates once** with a stricter
instruction, else falls back to the move's template text. The check is cheap and
deterministic, and it targets the 66% failure mode directly.

### 5.5 Interactive widgets (`WidgetFactory` registry)

Widgets appear **inline in the transcript** (not the side canvas). They are short, part of
the dialogue, and answered right there. Each is a `LearnWidget` built on the `Question` IR,
so grading, shuffling and "answer never shipped" come from `quiz_play.py`:

| Widget | Built on | Use |
|--------|----------|-----|
| `mcq` (distractors tagged with misconceptions) | mcq | quick check; a distractor exposes a misconception |
| `true_false_why` | true_false + short explanation | check + metacognition |
| `fill` / `numeric` | fill_blank / numerical | recall, calculation (tolerant grading) |
| `order` | ordering | processes, steps (e.g. stages of the water cycle) |
| `categorize` (drag into bins) | matching (bins as right-hand side) | biotic/abiotic, acid/base… |
| `match_pairs` | matching | term ↔ definition (a small puzzle) |
| `predict_reveal` | mcq + reveal text | "What will happen if…?" → commit → reveal (curiosity) |
| `spot_the_error` | mcq over the steps of a worked solution | find the faulty step (misconceptions) |
| `confidence` | 1–5 slider | metacognition; calibrates the policy (confident + wrong = strong misconception) |
| `explain_back` | short_answer, `LLMGrader` | "explain it in your own words" (partial credit) |
| `flashcards` (end of session) | cards from expectations | review deck, saved as a normal `ChatArtifact` |

Gamification is deliberately light: a **progress path** (objectives as stepping stones, from
the `learn_state` event), a "mastered" badge per objective, a streak counter for consecutive
correct checks, and an end-of-session summary card. No leaderboards (minors, one-to-one
context).

Check items are **pre-generated at plan time** and validated (the answer is in the options,
distractors are distinct, the question targets an expectation), so a check never waits on an
LLM call. `WidgetFactory` can also build one on the spot (e.g. a transfer problem) through
the same validation.

### 5.6 Keeping it alive: weaknesses of the first draft and the fixes

A final review of §5.1–5.5, asking: *where would a real 13-year-old get bored or feel
pushed around?*

| # | Weakness in the first draft | Why it hurts | Fix (built into the policy) |
|---|------------------------------|--------------|------------------------------|
| W1 | **Interrogation loop**: every tutor turn is a question. | Feels like an exam. Novices who know nothing just reply "idk" again and again. | **Ask/tell balance.** At most 2 questions in a row without giving something back (a short explanation, an insight, an example). The `explain_chunk` move teaches one small idea, then asks. |
| W2 | **Diagnose-first for everyone.** | Research on the *expertise reversal effect*: novices learn more from worked examples first; questioning first suits students who already know a bit. | **Novice path.** A first-turn "idk" or low stored mastery → *I do → we do → you do*: short explanation + example → guided question → independent check. |
| W3 | **Same widget again and again** (MCQ, MCQ, MCQ). | Repetition kills interest. | **Widget rotation.** Never the same widget kind twice in a row. The kind is chosen to suit the content (process → order, pairs → match, cause/effect → predict). |
| W4 | **No student control.** | Agency drives motivation. Typing "give me a hint" is effort. | **Quick-reply chips** every turn: 💡 Hint · 📖 Example · ✅ I know this · ⏩ Faster · 🐢 Slower. They are deterministic actions (no tracer call, no ambiguity). |
| W5 | **Fixed pace.** | Fast students get bored by pumps and hints. Slow students drown. | **Pace adaptation.** Two confident correct answers in a row → *fast track* (skip pumps, go to the check, then a ⭐ challenge question). Struggling → smaller steps, more worked steps. The "Faster/Slower" chips set it directly. |
| W6 | **Repetitive feedback** ("Great job!" every time). | Generic praise soon sounds fake. | The renderer gets the openings of its last 3 replies and is told not to repeat them. Praise the *reasoning* ("you used the energy idea, nice"), not the person. |
| W7 | **Pre-written hint ladder.** | Pre-written hints often don't match the actual mistake. | Hints are **written on the spot from the student's actual wrong answer**. The ladder *level* only limits how much may be revealed. Pre-written hints are a fallback. |
| W8 | **Curiosity only at the start** (one hook). | Interest drops after the first minute. | **A hook per objective**: each new objective opens with a mini-puzzle or a predict-then-reveal question, not "Now let's learn O2". |
| W9 | **Strictly linear order.** | It feels like a railway track. | **Choice at branch points.** When the next objectives don't depend on each other: "Next: chlorophyll or stomata, you choose." A tangent that matches a later objective → offer to go there (pull-forward). |
| W10 | **Boredom not detected** (only frustration). | A bored student gives one-word answers and leaves. | **Low-effort signal**: 3 very short or "ok/k" replies in a row → switch to a challenge, a predict widget or a puzzle instead of another question. |
| W11 | **Answers to later objectives ignored.** | The tutor seems not to listen. | Early credit (§5.1). |
| W12 | **"Stop" is the only exit.** | Long sessions tire students. | After each objective: *"Keep going or take a break? Your progress is saved."* Resume later with a quick warm-up question. |

These rules don't make the design more rigid, because they are **more ways to react**,
still chosen by code from what the student does. The structure keeps the tutor on track;
the variety stops it from feeling scripted.

---

## 6. API, events, persistence

**Requests** (backwards compatible):
```python
class ChatMessageRequest(BaseModel):
    message: str | None = Field(default=None, max_length=8000)   # was required
    mode: FilterMode | None = None
    filters: dict | None = None
    widget_response: WidgetResponse | None = None   # {widget_id, response, confidence?}
    # validator: exactly one of message / widget_response
```
A widget answer goes through the **same streamed turn**, so the tutor reacts
conversationally ("Nice! Now why…?"). The answer is graded server-side before the tutor
replies. The persisted user message stores a readable rendering ("Picked: B") plus the
structured response in `meta`.

**Endpoints**
- `POST /conversations/{id}/learning-sessions` `{topic?, objective_hint?}` → session (status `planning`); planning streams over SSE.
- `GET /learning-sessions/{id}` → plan outline + states (no hidden fields).
- `POST /learning-sessions/{id}/pause|resume|end`.
- `GET /me/learning` → mastery overview + "due for review" (feeds a future "practice weak areas").

**SSE events** (added to `docs/CHAT_EVENTS.md`): `learn_state` (progress path: objectives,
statuses, active cursor, phase) · `learn_widget` (play view of a widget, no answer) ·
`learn_feedback` (grading result for a widget turn) · existing `status`/`token`/`sources`/
`done`. The frontend adds `features/chat/learn/` (a `LearnWidget` renderer registry, like
`ARTIFACT_RENDERERS`, plus a `ProgressPath` component). Nothing in the transcript component
is special-cased.

**Tables** (`db/migrations/0003_learning_and_memory.sql`):
`learning_sessions` (id, conversation_id FK cascade, owner_id, status, plan jsonb, states
jsonb, cursor, phase, version, timestamps; one *active* session per conversation through a
partial unique index) · `learning_events` (append-only per-turn log: move, tracer output,
grading, retrieval trigger, used for eval/analytics and to rebuild state) · memory tables
(§7). Session updates use **optimistic concurrency** (`version`) because two tabs may send
turns.

---

## 7. The memory layer (normal chat + learn mode)

### 7.1 Layers

```
┌ L0 Working context ── assembled fresh each turn by ContextAssembler, token-budgeted
│
├ L1 Conversation ───── verbatim window (today) + STRUCTURED rolling summary
│                       store: chat_messages / conversations.summary(jsonb)
│
├ L2 Session state ──── LearningSession (plan, cursor, states, parking lot)
│                       structured, exact, NEVER passed through an LLM summary
│
└ L3 Long-term user ─── (a) profile facts   "prefers Nepali examples", "finds diagrams helpful"
                        (b) mastery         per user × kc_ref: p_mastery, last_seen, due_at
                        (c) episodes        "03 Nov — photosynthesis: 3/4 mastered, stuck on X"
                        store: user_memories, mastery_records (Postgres; embeddings for recall)
```

**Why separate layers:** each has a different write rule (verbatim / summarised / code /
extracted), a different lifetime (turn / conversation / session / months) and a different
trust level. Mixing them is how memory systems end up wrong without anyone noticing.

### 7.2 Changes to what exists (L1)

1. **Move summarisation off the request path.** Today `maybe_summarize` runs *inside* the
   stream before `done`. It moves to a `MemoryJobQueue` port (`inline` for tests,
   `threadpool` default, `postgres` outbox later for multiple replicas). Latency drops and
   a failed summary can't affect a turn.
2. **Structured summary.** Instead of free text, keep `{goals, facts_established,
   open_questions, preferences, topics}` (JSON, one fast call, lenient parse). The synthesizer
   and router read the fields they need. The incremental "fold only what left the window"
   rule stays exactly as now.
3. **Learn-mode conversations summarise differently.** The tutor needs the session state
   (L2), not a story of it. In learn mode the rolling summary only keeps non-tutoring content
   (asides, preferences). Tutoring turns are summarised into the `learning_events` log /
   episode at session end.

### 7.3 Long-term memory (L3)

**Ports** (each with a registry entry and config):

| Port | Default | Responsibility |
|------|---------|----------------|
| `MemoryStore` | `postgres` (+ `memory` for tests) | CRUD for `MemoryItem`s and `MasteryRecord`s, owner-isolated; similarity search over a user's items |
| `MemoryExtractor` | `llm` (fast model) | from a finished turn/session, propose ops `ADD/UPDATE/DELETE/NOOP` against the top-k similar existing items (Mem0 pattern) |
| `MemoryPolicy` | `allowlist` | what *may* be stored: an allowlist of categories + PII/sensitive denylist; can veto any op |
| `MemoryRecall` | `hybrid` | per turn: the always-on profile slice + top-k relevant items for the standalone query + mastery for the topic |
| `ContextAssembler` | `budgeted` | builds the final prompt context from all layers with a priority order and a token budget |

```python
class MemoryItem(BaseModel):
    id: str; owner_id: str
    kind: Literal["preference","learning_need","interest","goal","episode"]
    content: str                       # one short, self-contained statement
    embedding: list[float] | None
    source: MemorySource               # conversation_id, message_ids / session_id (provenance)
    confidence: float
    valid_from: datetime
    superseded_by: str | None          # temporal validity (Zep-style, no graph DB)
    last_used_at: datetime | None
    expires_at: datetime | None        # retention (episodes decay; config)

class MasteryRecord(BaseModel):         # written ONLY by LearnerModel / quiz grading (code)
    owner_id: str; kc_ref: str
    p_mastery: float; attempts: int; correct: int
    last_seen: datetime; due_at: datetime       # review schedule (Leitner/FSRS-lite)
    stability: float                            # forgetting-curve parameter
```

**Write path (async, never on the request path):**
```
turn/session finished ─► MemoryJobQueue
   ├─ L1 summary fold
   ├─ MemoryExtractor: candidate facts + top-k similar existing items ─► ops
   │     └─ MemoryPolicy: allowlist / denylist / confidence floor ─► MemoryStore (txn)
   └─ (learn) LearnerModel.commit(session) ─► MasteryRecord upsert + episode MemoryItem
```
- **Mastery also comes from quizzes**: `QuizSessions.finish` already records per-question
  results. Tagging questions with `kc_ref` (where the corpus has `standard`) lets ordinary
  quiz attempts update mastery too. One learner model for every surface.
- **Context, not just the message.** The extractor also receives the previous exchange (the
  tutor's last reply, up to ~800 chars, and the student's message before it), so "I like the
  above style" or "yes" (to "shorter answers from now on?") can be understood. That context
  is **for understanding only**: a memory records only what the student stated or agreed to.
  A note must be self-contained; the policy vetoes unresolved references ("likes the above
  style"). Short replies are read only when they answer a question the tutor just asked.
- Extraction is **conservative**: only durable, user-stated or repeatedly observed facts.
  "The student said they are in grade 8" is not stored (that comes from `UserContext`,
  which is authoritative). "Student asked about mitochondria" is not a memory (it's in the
  transcript).

**Read path (per turn, cheap):**
- Profile slice (≤ 5 items, cached per conversation, invalidated on write): always injected.
- Relevant items: embedding similarity against the **standalone** query (already computed by
  the transformer), top-3 above a threshold. Retrieval is per-user, so it is a small set
  (hundreds of items). The Postgres impl can use pgvector, or brute-force cosine in Python
  behind the same port. **No new infrastructure needed.**
- Learn mode: mastery for the plan's `kc_ref`s (plan seeding, §5.2) + latest episode on the
  topic ("Last time you got stuck on light reactions, want to start there?").

**ContextAssembler priority** (highest first, each with a budget share in config):
system/persona → current move (learn) → hidden answer key (learn) → pinned/ephemeral
evidence → L2 session recap → recent verbatim window → profile slice → relevant memories →
L1 summary. When over budget, it trims from the bottom. Memory **personalises, it never
grounds**: memories are marked as "about the learner", never treated as facts about the
subject, and never cited.

### 7.4 Governance (these are minors, so this part is required)

- **Data minimisation**, the same rule as `UserContext`: the denylist (enforced in
  `MemoryPolicy`, unit-tested) blocks names, contact details, family/health/religion/
  caste/location below district, marks/attendance, anything about other people. Memory is
  **never** read by an access decision.
- **Visible and editable:** "What LessonForge remembers about me". `GET /me/memories`,
  `DELETE /me/memories/{id}`, `DELETE /me/memories` (clear all), and a per-user
  `memory_enabled` switch (school-level default from the integration config).
- **Forget cascades:** the existing `lessonforge-admin forget` also deletes
  `user_memories`, `mastery_records`, `learning_sessions`, `learning_events`.
- **Retention:** episodes expire (default 180 days); preferences have no TTL but are
  superseded; `last_used_at` makes it possible to prune unused items.
- **Isolation:** every query is keyed by `owner_id` (and tenant/space from
  `request_context`). There is a cross-tenant test for each store, like the existing
  Postgres store tests.
- **Audit:** memory writes/deletes emit content-free activity events (`memory.written`,
  `memory.deleted`), following the `activity/events.py` rule (no content in analytics).

---

## 8. Evaluation (how we know it teaches)

1. **Deterministic CI (no LLM):** policy transition tests (table-driven: state + tracer
   output → expected move), plan validators, leak-guard cases, retrieval-trigger tests
   ("off-topic never retrieves", "queries always contain the objective"), memory policy
   denylist tests, store isolation tests. Fakes for every port.
2. **Simulated-student harness** (`lessonforge.eval.tutor`, the TutorGym/MathDial approach):
   LLM student personas with seeded misconceptions, a knowledge level and a style (terse /
   tangent-prone / "just tell me" / frustrated). Each run is scored on:
   - **answer-leak rate** (solution revealed before an attempt): target < 5%
   - **objective completion** and **turns-to-mastery**
   - **drift rate**: % of tutor turns not about the active objective (an LLM judge plus the
     `learning_events` move log)
   - **groundedness**: tutor claims supported by pinned evidence
   - **interaction cadence**: widgets per N turns, max consecutive text-only turns
   - **LearnLM rubric** (active learning, cognitive load, adaptivity, curiosity,
     metacognition), scored by an LLM judge and spot-checked by teachers
   A gate on the deterministic metrics in CI, and a live run on demand (`--generate`, like the
   existing eval harness).
3. **Product telemetry** (content-free): sessions started/completed, bottom-out rate, checks
   passed on first try, resumption rate, mastery gain per kc across sessions.

---

## 9. Code layout (SOLID check)

```
domain/learning.py                  plan, objective, session, widgets (pure data + validators)
domain/memory.py                    MemoryItem, MasteryRecord
services/learn/
  planner.py        LessonPlanner (ABC) · LLMPlanner · registry
  tracer.py         StudentStateTracer (ABC) · LLM / Rules / Hybrid
  policy.py         PedagogicalPolicy (ABC) · GraphPolicy (YAML) · Scripted
  retrieval.py      RetrievalPolicy (ABC) · AnchoredRetrievalPolicy (wraps ChatRetriever)
  renderer.py       TutorRenderer (ABC) · LLMRenderer · move prompt templates
  guard.py          AnswerLeakGuard (ABC) · RulesLeakGuard
  widgets.py        WidgetFactory registry (@register_widget) · validation
  learner.py        LearnerModel (ABC) · BKTLearnerModel
  session_store.py  LearningSessionStore (ABC) · Memory / Postgres
  handler.py        LearnTurnHandler — composition only, like ChatPipeline
services/chat/handlers.py           TurnHandler (ABC) · QATurnHandler · SkillTurnHandler
services/chat/skills/learn.py       the `learn` skill (entry point; starts a session)
services/memory/
  store.py · extractor.py · policy.py · recall.py · assembler.py · jobs.py
config/pedagogy/scaffolded.yaml     default strategy graph
```

- **S:** each class decides or does one thing (the tracer classifies, the policy decides,
  the renderer phrases, the guard checks).
- **O:** new widget = `@register_widget`; new strategy = YAML; new tracer/planner/store =
  `@register_*` + config. `ChatPipeline` gets new behaviour through `TurnHandler`s, without
  growing its `if` chain.
- **L:** every port has a fake that passes the same contract tests as the real one
  (the pattern already used for `VectorStore`, `ChatStore`).
- **I:** `LearnTurnHandler` depends on narrow ports, not on `Container`. The renderer
  never sees the store. The tracer never sees retrieval.
- **D:** handlers depend on ABCs. `container.py` builds them from config (`learn:` and
  `memory:` blocks). Kill switches: `learn.enabled: false`, `memory.long_term.enabled: false`.

```yaml
learn:
  enabled: true
  strategy: scaffolded            # config/pedagogy/<strategy>.yaml
  planner:   {provider: llm, max_objectives: 5, lazy_detail: true}
  tracer:    {provider: hybrid}
  policy:    {provider: graph, max_hints: 3, widget_every: 3, tangent_budget: 2}
  retrieval: {provider: anchored, coverage_threshold: 0.35, pin_floor: 0.3}
  guard:     {provider: rules}
  learner:   {provider: bkt, p_init: 0.2, p_learn: 0.15, p_slip: 0.1, p_guess: 0.2, mastered: 0.85}
  session:   {idle_pause_minutes: 30}
memory:
  jobs:      {provider: threadpool}
  summary:   {structured: true}
  long_term: {enabled: true, extractor: llm, policy: allowlist, recall_top_k: 3,
              episode_ttl_days: 180, profile_slice: 5}
```

---

## 10. Cost and latency per turn

| Turn type | LLM calls | Retrieval |
|-----------|-----------|-----------|
| Session start | 1 planner (reasoning model) + the transformer's multi-query | T0 once |
| Free-text tutor turn | 1 tracer (fast) → 1 renderer (streamed) | none (usually) |
| Widget answer turn | 0 tracer · 1 renderer (or a template for simple feedback) | none |
| Background | summary fold + memory extraction (fast, async) | — |

Optional `tracer: inline` mode makes the renderer return the trace and the reply together
(one call) for very cheap deployments. Less reliable, so not the default.

---

## 11. Edge cases

| Situation | Behaviour |
|-----------|-----------|
| "I already know this" | `skip` → a diagnostic `check`; pass → mark mastered and advance; fail → teach (said kindly) |
| "Just give me the answer" | at most one nudge → `bottom_out` with reasoning → transfer check; never refuse |
| Random off-topic | short acknowledgement → `park` → back to the objective; parking lot offered at wrap-up |
| New topic mid-session | confirm → pause the current session → plan a new one (resume chip on the old) |
| Corpus has nothing on the topic | plan with `scope.ungrounded=True`, show "not from your textbook" in the UI, more conservative tone, no invented citations |
| Student is wrong and confident (confidence widget) | treat as a strong misconception → `confront` with a contrast case |
| Student returns next day | resume: recap line + spaced `check` on the last mastered objective, then continue |
| Two tabs | optimistic concurrency on `learning_sessions.version`; the losing turn gets a refresh |
| LLM down mid-session | tracer → `rules`; renderer → move template text; the session stays consistent because state is structured |
| Teacher uses learn mode | allowed (preview what students get); doesn't write mastery unless the role is student (config) |
| Nepali-medium student | language from `UserContext.medium`; expectations carry NE keywords; widgets render Devanagari |

---

## 12. Phased delivery

| Phase | Scope | Exit criteria |
|-------|-------|---------------|
| **P1 Core tutor** | `TurnHandler` refactor (no behaviour change, existing tests green) · domain + session store + migration · planner (lazy) · tracer · graph policy · renderer · leak guard · anchored retrieval · widgets: mcq, fill, numeric, order, predict_reveal · progress path · entry by toggle + `learn` skill | deterministic suite green; simulated-student leak rate < 5%, drift < 10% on the seed topics; browser-verified E2E |
| **P2 Memory** | async job queue · structured summary · `MemoryStore`/`Extractor`/`Policy`/`Recall`/`Assembler` · `/me/memories` UI · forget cascade · mastery from learn + quiz | isolation + denylist tests; recall improves personalised-answer eval; zero PII in a red-team set |
| **P3 Richer interaction** | categorize, match_pairs, spot_the_error, confidence, explain_back, end-of-session flashcards · spaced review on resume · `GET /me/learning` | widget validation suite; teacher review of generated checks |
| **P4 Scale and insight** | second pedagogy strategy (worked-example-first for lower grades) · postgres outbox for jobs · teacher dashboards from mastery (aggregate, consented) · A/B by strategy | live eval run on ≥ 30 topics; first classroom pilot |

---

## 13. Open questions for the product owner

1. **Default entry:** explicit toggle only, or also let the router switch automatically
   when confidence is high? (Recommended: toggle + router *offers*, never switches on its own.)
2. **Memory default for students:** on or off by default per school? (Recommended: on, but
   visible and clearable, school can disable.)
3. **Teacher visibility:** may teachers see per-student mastery from learn mode, or only
   class aggregates? (Affects consent copy and the dashboard in P4.)
4. **Session length target:** 3–5 objectives (~15 min) by default? Shorter for lower grades?

---

---

## 14. Implementation notes (2026-11-03)

**Built:** §3–§7 and the §5.6 fixes. Code: `domain/learning.py`, `domain/memory.py`,
`services/learn/` (planner, tracer, learner, policy, retrieval, renderer, guard, widgets,
store, service), `services/memory/` (jobs, store, policy, extractor, service),
`services/chat/skills/learn.py`, `api/routers/memory.py`, migration
`0003_learning_and_memory.sql`, and the web UI in `web/src/features/chat/learn/`.

**Where the build differs from the proposal, and why:**

| Proposal | Built | Why |
|---|---|---|
| Policy graph in a YAML file (§5.3) | A registered strategy class (`ScaffoldedPolicy`); every threshold is config (`chat.learn.policy`) | The rules test conditions over structured state (coverage, attempts, streaks, prerequisites). In YAML that needs a small rule language, which is harder to test and review than table-driven Python tests. A new strategy is one class plus `POLICY_REGISTRY`. |
| In-session router catalogue (§2.1) | The tracer also recognises `stop / skip / switch_topic` | One classification call per turn instead of two |
| Widget answers through `/message` | A separate `POST …/learn` endpoint | The v1 `/message` contract stays unchanged (the contract guard flagged it), and each request type stays small |
| Background prefetch of the next objective (§4) | Detailed when the cursor arrives (status "Preparing the next part…") | Simpler and correct first. Prefetch is an optimisation. |
| Tutor reply streamed token by token | The reply is generated whole, checked by the leak guard, then shown | A reply that has already streamed can't be taken back. Replies are short (~80 words), about 2–3 s on gemma. |
| Router may start lessons | The router **offers** (`learn_offer`) unless the student says "teach me / step by step / help me understand" | §13 Q1 recommendation |

**Fixed after the first user trial: a check loop.** An objective with 2 checks, one missed,
could never reach `p ≥ 0.85`. `pick_check` then reused checks, and the client keyed grading
by check id, so a re-shown check arrived pre-graded and locked. Fixes:
- a check is never shown twice;
- `enough_evidence` lets the lesson move on (two passes, or one pass once every check is used);
- with every check used and none passed, the planner makes a `fresh_check`, or the lesson moves on;
- every widget display gets its own `instance_id`;
- "I know this" after a passed check moves straight on;
- "move on" / "next" is honoured and the objective is recorded as `left` (revisited by spaced
  review, never counted as mastered);
- a `max_turns_per_objective` cap is a safety net.

Regression tests replay the trial's exact sequence.

**Follow-ups (not built yet):**
- the simulated-student eval harness (§8.2);
- quiz attempts → mastery (quiz questions carry no `kc_ref` yet);
- content-free activity events for memory writes;
- a Postgres-outbox job queue for multi-replica ordering;
- background prefetch of the next objective.

**Verified:**
- 1012 backend tests pass, including every new store against real Postgres and the `forget`
  cascade test. ruff is clean.
- The web gate passes (`npm run verify`: API drift check, types, lint, 37 tests, build).
- Live headless-browser runs through the fake Edboox with **gemma4:31b-cloud**:
  - a real plan came back; objectives and checks are in the student's medium (Nepali);
  - early credit worked (⭐ on a later objective);
  - off-topic questions were parked;
  - a lesson pulled toward an unrelated subject was steered back to its goal;
  - wrong answers led to a new explanation with an everyday example;
  - the hint ladder ended in a fill-in-the-blank hint;
  - memory extraction kept "prefers football examples" and stored neither the name nor the phone number.
- Live runs found three prompt bugs, all fixed with regression tests: checks were in English
  for a Nepali-medium student; the tutor restated the widget's question; the tutor praised an
  answer the student never gave after a button press.

### Sources

- Graesser et al., AutoTutor / EMT dialogue — [FLAIRS 2005](https://cdn.aaai.org/FLAIRS/2005/Flairs05-085.pdf), [Graesser 2016 review](https://link.springer.com/doi/10.1007/s40593-015-0086-4)
- Puech et al., *Pedagogical Steering of LLMs for Tutoring* (StratL) — [arXiv 2410.03781](https://arxiv.org/abs/2410.03781v2)
- Wang, Demszky et al., *Bridging the Novice-Expert Gap* (Bridge) — [NAACL 2024](https://aclanthology.org/2024.naacl-long.120)
- Macina et al., *MathDial* — [arXiv 2305.14536](https://arxiv.org/abs/2305.14536), data: github.com/eth-nlped/mathdial
- Bastani et al., *Generative AI Without Guardrails Can Harm Learning* — [SSRN](https://papers.ssrn.com/abstract=4895486), [Wharton summary](https://knowledge.wharton.upenn.edu/article/without-guardrails-generative-ai-can-harm-education)
- Kestin et al., *AI tutoring outperforms in-class active learning* — Sci. Reports 2025, doi 10.1038/s41598-025-97652-6
- LearnLM Team, *LearnLM: Improving Gemini for Learning* — [arXiv 2412.16429](https://arxiv.org/abs/2412.16429v3); [LearnLM-Tutor](https://arxiv.org/html/2407.12687v3)
- OpenAI, [ChatGPT Study Mode](https://openai.com/blog/chatgpt-study-mode/); Anthropic, [Claude for Education / Learning mode](https://www.anthropic.com/news/introducing-claude-for-education)
- Khan Academy, [Khanmigo math & tutoring updates](https://blog.khanacademy.org/khanmigo-math-computation-and-tutoring-updates/), [7-step prompt engineering](https://blog.khanacademy.org/khan-academys-7-step-approach-to-prompt-engineering-for-khanmigo)
- SocraticLM — [github.com/CMLF-git-dev/SocraticLM](https://github.com/CMLF-git-dev/SocraticLM); TutorGym — [arXiv 2505.01563](https://arxiv.org/pdf/2505.01563)
- Knowledge tracing + LLMs — [TutorLLM, arXiv 2502.15709](https://arxiv.org/pdf/2502.15709)
- Agent memory: Mem0 / Letta / Zep comparisons — [overview](https://dev.to/agdex_ai/letta-vs-zepgraphiti-vs-mem0-choosing-an-ai-agent-memory-architecture-155a), [glossary](https://klu.ai/glossary/agent-memory-architectures)
