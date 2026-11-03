# LessonForge Web — design & architecture

The React/TypeScript client in [`web/`](../web). It replaces the plain-HTML pages
that lived in `src/lessonforge/api/static/`. This document is the source of truth
for **why it looks and behaves the way it does** and **how it is put together**, so
the next screen (or Edboox adopting the code) follows the same rules.

---

## 1. Who it is for (design inputs)

| Input | Consequence |
|---|---|
| Teachers in Nepal, mostly on **phones**, on **metered data** (CBS 2021: 73 % of households own a smartphone, 56 % use the internet; ~half of users find data expensive). | Mobile-first layouts; bottom tab bar on phones; route-level code splitting; **no web fonts** (system serif/sans; Android already ships Noto Devanagari); markdown/math libraries load only with Chat. |
| Lessons are **bilingual** (`en`, `ne`, `en-ne`). | Every font stack ends in a Devanagari face; line-height 1.55 for conjuncts; inputs are 16 px (no iOS zoom). |
| The **LDD is the source of truth**; every export compiles from it. | The editor *is* the document — no separate "preview" mode. What you see is what exports. |
| Generation is **slow** (one or more LLM calls, 20–90 s). | Honest progress: an elapsed timer and the pipeline stages explained. We never fake a percentage. Requests that can't be undone (generate) are never fired twice. |
| AI rewrites must be **reviewable**. Tools like Tiptap's review mode and Cursor show that people want to accept or discard each change. | AI changes always come back as a **proposal** (before/after per section, score delta) that the teacher accepts or discards. If the draft changed while the AI was working, the proposal is marked stale. |
| Edboox may adopt this UI. | Loosely coupled layers, a pluggable auth strategy and API base, routes that can be mounted on their own, and no global CSS leakage (see §6). |

### What the old UI got wrong (flow audit)

- Generating a lesson **didn't save it**. Only units reached the Library, and a refresh lost the lesson. **Now:** creating a lesson persists it (`POST /documents/lessons`) and opens `/lessons/:id`.
- Guardrails were checked **only on a button press**. **Now:** validation runs on each edit (debounced, no LLM cost), and problems show next to the field and in the outline.
- Creating things was **spread over three pages**, with "just a quiz" buried under the lesson form. **Now:** there is one composer on Home. You pick *what* (lesson · unit · quiz · worksheet · slides), and every flow continues from the same brief.
- The UI used emoji as its iconography, and walls of open form fields. **Now:** a document-like editor with an outline, quiet controls that only show on hover or focus, and progressive disclosure.

## 2. Visual language — "the plan book"

The metaphor is a teacher's plan book: paper, ink, a pen for the teacher's work, a
highlighter for a colleague's (the AI's) suggestions. It deliberately avoids the
generic-AI look: no purple gradients, no glass, no sparkle icons, no emoji as UI.

- **Surfaces:** plain white paper (`--paper`) and a barely-grey margin (`--canvas`) in light mode; neutral near-black in dark mode. Surfaces are deliberately colourless (no cream/warm tint) so the accents carry all the colour. Sections are separated by hairline rules and whitespace instead of floating shadowed cards.
- **Ink:** neutral near-black ink for text, and muted grey ink for metadata.
- **Pine** (`--pine`), the single action colour, for primary buttons, focus rings and links.
- **Marigold** (`--marigold`), the highlighter, *only* for AI proposals and "needs attention" markers. Because it means one thing, the eye learns that marigold = "look here".
- **Brick** (`--brick`) for errors and destructive actions.
- **Type:** headings use the system serif (Charter / Iowan / Georgia → Noto Serif Devanagari), which reads as a *document*; UI text uses the system sans. Numbers use tabular figures.
- **Shape:** 6–8 px radii, 1 px rules, almost no shadow except on overlays.
- **Signature element:** the **phase timeline**, a proportional bar of the lesson's phases against its duration. It shows pacing and over- or under-run at a glance in the editor, unit days, and the library.
- **Motion:** 120–180 ms ease-out on disclosure and overlays only; `prefers-reduced-motion` turns it off.
- **Dark mode:** the same tokens redefined (follows the OS; can be overridden with `data-theme`).

## 3. Information architecture

```
/                       Home: the composer (what are you teaching?) + recent work
/lessons/:id            Lesson editor (saved document; read-only for a colleague's shared plan)
/units/new              Unit planner: brief → editable arc + syllabus coverage → write all days
/units/:id              Unit overview: days timeline, coherence, export, unit test
/units/:id/days/:day    Lesson editor bound to one day of a unit
/studio/:kind           Quiz / worksheet / slides studio (from a brief, a lesson or a unit)
/library                My work · Shared in my school
/chat[/:conversationId] Grounded chat, citations, interactive quizzes
/corpus                 Knowledge base (curators)
/admin/*                Owner console
/settings               Teacher profile (defaults, voice, local anchors)
```

Top-level paths match the server's `/me.nav` contract, so the server stays the
authority on who can see what. Each route declares the permission it needs. A user
who opens a route they can't use is redirected to the first one they can (a
student only ever sees Chat).

**Navigation:** a left rail on ≥ 900 px and a bottom tab bar on phones. When a user
has a single destination, the nav is hidden entirely.

## 4. Key interactions

**Lesson editor**: a three-pane layout that collapses on phones:
1. *Outline* (left): each section shows its status (complete / needs attention / guardrail error), plus the phase timeline. Clicking a section scrolls to it.
2. *Document* (centre): every section is editable in place. Fields look like text until hovered or focused. Lists of short items are chips; sentence-length items are auto-growing lines.
3. *Inspector* (right sheet): **Checks** (live guardrails), **Quality** (rubric meters, re-score), **Sources** (retrieval tree), **History** (versions, server undo/redo).

The top bar shows the save state (`Saved` · `Unsaved changes` · `Saving…`),
local undo/redo, Save (⌘/Ctrl-S), Export, and "Make a quiz/worksheet from this
lesson". Unsaved work survives a reload: a local draft is kept per document and
version, and you're offered to restore it. Leaving the page with unsaved changes asks first.

**Revise with AI**: available on every section and on the whole lesson. You write an
instruction, and the proposal returns as a review panel: score delta, cascaded
sections, and before/after for each section. *Accept* applies it to the draft (which
then still has to be saved), *Discard* drops it.

**Long generation**: buttons switch to a progress state with an elapsed timer and
the stages described. The request can't be double-fired, and failures keep your brief.

## 5. Architecture

```
web/src
├── app/          shell, route table, providers, guards (composition only)
├── lib/          framework-agnostic plumbing
│   ├── api/      transport (auth strategy, error envelope, SSE, downloads) + generated contract types
│   ├── hooks/    useDraft (immer + undo/redo), useDebouncedValue, useHotkey, useUnsavedGuard, …
│   └── markdown/ the safe Markdown + math + code renderer (lazy)
├── ui/           design system: tokens + primitives, no business logic, no API calls
└── features/     one folder per product area; each owns its API hooks, model, components, routes
    ├── session   (who am I, permissions)     ├── lessons   (editor, section registry)
    ├── create    (Home composer)             ├── units     (planner, overview)
    ├── studio    (quiz/worksheet/slides)     ├── library
    ├── grounding (sources tree, shared)      ├── chat      (+ artifact renderer registry)
    ├── knowledge (corpus)                    ├── admin
    └── profile
```

Rules (lint-enforced where possible):

- **Dependency direction:** `app → features → ui, lib`. `ui` and `lib` never import a feature. A feature may import another feature only through that feature's `index.ts` public surface.
- **Server state** lives in TanStack Query (cache keys per feature in `queryKeys`). **Document drafts** live in `useDraft` (immer patches give cheap undo/redo and dirty tracking). There is no global store.
- **Types come from the contract:** `npm run gen:api` turns `docs/openapi.v1.json` into `lib/api/schema.d.ts`. Where the backend returns an untyped `dict`, the shape is written once in that feature's `types.ts` and marked `// contract gap`, so it can be deleted when the server declares a response model.
- **Extensibility through registries, not conditionals:**
  - `features/lessons/model/sections.ts`: one entry per LDD section (title, refine target, summary, status, component). The outline, validation mapping and editor all read from it.
  - `features/chat/artifacts/registry.ts`: `kind → lazy renderer` (quiz today).
  - `features/chat/artifacts/quiz/widgets.tsx`: `widget → input component`.
  - `features/studio/kinds.ts`: `kind → editor + capabilities`.
  - `app/routes.tsx`: `{path, perm, lazy}`.
- **Persistence adapters:** the lesson editor doesn't know where a lesson lives. It takes a `LessonSource` (`load`, `save`, `readOnly`, labels). A saved document and a unit day are two small adapters, so Edboox can bind the editor to its own storage.

## 6. Embedding / Edboox portability

- `createLessonForgeClient({ baseUrl, auth })`. `auth` is a strategy: `cookieSession()` (today: same-origin cookie + `X-LF-Request` CSRF header, redirect to `login_url` on 401) or `bearerToken(getToken)` (Edboox calling with its own tokens).
- `<LessonForgeProvider config>` wires the client, query cache and toasts. Each feature exports its routes/pages, so a host app can mount `/lessons/:id` alone.
- `basename` is configurable; nothing assumes it owns `/`.
- Styles are CSS Modules. Design tokens are custom properties on `[data-lf-root]` (and `:root` in the standalone shell), so they don't leak into a host page.
- Build output is plain static files. FastAPI serves them (SPA fallback), but they can sit behind any CDN.

## 7. Serving, build, quality gates

- `web/` builds to `src/lessonforge/api/webui/` (git-ignored, packaged). Docker builds it in a Node stage and points `LF_WEB_DIST` at it. FastAPI serves `/assets/*` immutable-cached and every app route as `index.html` (`no-cache`).
- Dev: `npm run dev` (Vite on :5173, proxying `/api`, `/auth`, `/health` to :8000).
- Gates: `npm run typecheck`, `npm run lint`, `npm test` (Vitest + Testing Library + MSW), `npm run build`, and `npm run check:api` (generated types are up to date with the contract).
