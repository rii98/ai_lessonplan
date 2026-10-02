# Chat skills & intent routing

The chatbot began as one capability: *answer a question from the knowledge base*.
This layer turns it into a **platform of capabilities ("skills")** behind one input box.
The first skill is the interactive **quiz**; the foundation is meant for many more.

```
user message
   │
   ▼
IntentRouter ──► qa (default)  ──► query transform → retrieve → synthesize   (unchanged)
   │
   └──► a Skill (quiz, …) ──► status / token / artifact / awaiting events
                                   │
                                   └─► ChatArtifact (persisted) ──► side canvas + history
```

## Intent detection (`services/chat/intent.py`)

Layers, cheapest first — so the common case (a plain question) costs **nothing extra**:

| # | Layer | Cost | What it does |
|---|-------|------|--------------|
| 1 | **Awaiting** | free | The last assistant turn asked for a missing slot ("What topic?") → a short reply goes back to that skill. "cancel / never mind" drops it. |
| 2 | **Follow-up** | free | "another one", "harder" right after a skill ran → back to that skill. |
| 3 | **Lexical gate** | free | Each skill contributes weighted regex triggers (English, Romanised and Devanagari Nepali). Nothing above the gate → plain question, **no LLM call**. |
| 4 | **LLM classifier** | 1 fast-model call, *only if gated* | Sees the skill catalogue (descriptions, examples, **counter-examples**), recent turns, and artifacts already made; returns `{intent, confidence, slots}`. Resolves "quiz me on *this*", tells "what is a quiz?" (a question) from "quiz me" (a command). |
| 5 | **Threshold** | free | Below `min_confidence` the skill does **not** run — answering is safer than acting on a guess. |

Providers (`chat.intent.provider`): `none` (kill switch) · `rules` (deterministic, free) ·
`hybrid` (default). `chat.intent.gate: always` classifies every turn (max recall).

Every decision is a `RoutedIntent` — streamed as an `intent` SSE event and stored on the
assistant message (`meta.intent`): the raw material for **measuring and improving routing**.
`tests/test_chat_intent.py` holds labelled utterance sets (commands / ambiguous / plain /
look-alikes) — grow them with every skill; they are the routing regression suite.

## Adding a skill (the whole point)

```python
# services/chat/skills/flashcards.py
SPEC = SkillSpec(
    name="flashcards",
    description="Make a deck of flashcards the student can flip through.",
    examples=("make flashcards for the water cycle",),
    counter_examples=("what is a flashcard?",),
    slots={"topic": "...", "count": "..."},
    triggers=(Trigger(r"\bflash\s*cards?\b"),),
)

@register_skill
class FlashcardsSkill(Skill):
    spec = SPEC
    def run(self, ctx):
        yield ChatEvent("status", "Making your flashcards…")
        yield ChatEvent("artifact", ChatArtifact(conversation_id=ctx.conversation.id,
                                                 kind="flashcards", title=..., payload=...))
        yield ChatEvent("token", "Here's your deck — open it on the right.")
```
Import it in `skills/__init__.py`. **No change** to the router (its classifier prompt and
lexical gate are generated from the specs), the pipeline, or the API. Add the artifact
kind to `ArtifactService` + a renderer entry in `chat_artifacts.js` (`RENDERERS`).

Other skills this unlocks: study plan, "explain it simpler", compare two topics, summarise
a chapter, translate to Nepali, lesson-plan generation from chat, "turn this into a
worksheet" (reuses the assessment exporters), voice/OCR homework help.

## Artifacts (`ChatArtifact`, `ArtifactAttempt`)

A skill's output is a first-class **artifact**, not message text: its own id, persisted in
the chat store (`chat_artifacts`, `chat_attempts`; memory/sqlite/postgres), linked to the
assistant message, shown as a card in the transcript and in the side canvas, and revisitable
any time. Many per conversation; many attempts per artifact.

## The quiz skill

- Reuses the **assessment foundation**: `AssessmentSpec` blueprint, type-aware retrieval,
  `QuizGenerator`, and the print exporters (a teacher can download what a student played).
- Slots → blueprint: `build_spec` (MCQ-heavy default mix scaling with count; user-named
  types split evenly; `explanations=True` so every question carries its "why").
- **Server-side grading** (`quiz_play.py`): the play view carries *no answers*; a question's
  answer is revealed only after it is answered. `RuleGrader` for objective types (typo-tolerant
  fill-ins, numeric tolerance, matching/ordering against the deterministic shuffle);
  `LLMGrader` judges typed short/long answers (`partial` credit), falling back to
  self-check if the model is down. Grading is a port (`AnswerGrader`).
- Attempts are rows → retake, history, review, **retry the missed ones** (no LLM).

## Where this goes next

- **Proactive intents**: after a QA answer, offer a skill ("Quiz me on this" is the first).
- **Embedding-similarity router** provider (utterance ↔ skill examples) as a third
  `IntentRouter` — better recall than regex, cheaper than an LLM call.
- **Skill chaining / planning**: "quiz me, then make flashcards of what I missed" — the router
  returning an ordered plan instead of one skill.
- **Learner model**: attempts already record per-question, per-type results; aggregate them
  per student/topic to drive "practice your weak areas" and teacher dashboards.
- **Per-user/per-role routing** (teacher vs student skills), and eval gates on the labelled
  utterance sets in CI.
