# Edboox integration — identity, access, user context, Postgres

> Status: **implemented** (all phases of §13, incl. 3b knowledge spaces, 3c lesson
> sharing and 7 activity/reporting). Implementation notes and the few places the
> build refined this design are in §18. Scope: connect LessonForge (the AI
> service) to Edboox (the school platform that owns users); gate features by role;
> restrict corpus ingestion to platform owners; use Edboox's knowledge of each user
> (grade, school, subjects…) to scope retrieval and personalise output; make
> Postgres the only persistent store; keep our UI swappable for Edboox's own UI.

## 1. Decisions in one screen

| # | Decision | Why |
|---|----------|-----|
| D1 | **Edboox authenticates, LessonForge authorizes.** Edboox says *who* the user is (signed token); LessonForge decides *what they may do* (its own role→permission policy). | No user/password store here, no shared database, one concern per side. |
| D2 | **Trust = JWT signed by Edboox, verified against its public keys (JWKS).** Asymmetric (RS256/EdDSA). | We hold no secret that can mint identities; key rotation is free (`kid`); no per-request call to Edboox. |
| D3 | **API-first: our UI is just the first client.** Every UI need is a public, versioned `/api/v1` endpoint with a CI-guarded OpenAPI contract. Edboox's future UI calls the very same API. | Moving to Edboox's UI becomes *their* frontend work, with zero backend change. |
| D4 | **Two auth carriers, one principal.** Bearer header (Edboox UI / backend) or a session cookie set by a one-time launch (our UI). No endpoint cares which. | Both UIs work at once, so migration can happen screen by screen. |
| D5 | **Superadmin is never asserted by Edboox.** It is a LessonForge-local grant, issued only by CLI and audit-logged. | A compromised or misconfigured Edboox can't escalate anyone to "rewrite the curriculum corpus". |
| D6 | **Permissions, not roles, in code.** Routes declare `require(Perm.AUTHOR)`; config maps roles → permissions. | Changing who-can-do-what is a config change; a new role is one YAML line. |
| D7 | **Owner comes from the principal, never the request.** `owner_id` params removed; a foreign resource returns `404`. | Closes today's IDOR hole without leaking whether a resource exists. |
| D8 | **User context: claims for *authorization*, directory for *personalisation*.** The token carries only what access decisions need; richer details (section, school, subjects taught, medium…) are pulled from an Edboox endpoint, cached, and refreshed out of band. | Small tokens; authorization never depends on a cache; Edboox data drives retrieval filters and defaults. |
| D9 | **One shared vocabulary for grade/subject/etc.**, used by both the Edboox mapper and corpus ingestion. | Metadata filtering only works when both sides say `"Science"`, not `"Science and Technology"` on one side. |
| D10 | **Postgres is the only persistent store** (one `DATABASE_URL`, one pool, versioned SQL migrations). `memory` stays for unit tests; `sqlite` is retired. Qdrant stays the vector store. | One dialect, real types, FKs, no schema drift. |
| D11 | **The fake Edboox is a separate service with its own DB.** It shares only HTTP and the documented contracts with us. | Pointing at the real Edboox is a config change. |
| D12 | **Usage is tracked as typed activity events** (who, which school, what action, what kind), never message text. Edboox **pulls** them through a cursor feed plus summary reports (§15). | Edboox can monitor usage, cost and engagement while students' words stay private, with no webhook delivery to maintain. |
| D13 | **Every chunk belongs to a knowledge space** (`global` or `school:<id>`), and every search carries a mandatory `AccessScope`, the list of spaces the caller may read. Shared curriculum lives in `global`; each school's private documents live in its own space. Physical placement (one shared collection vs. a collection per school) is a vector-store config switch (§16). | Today's rule is enforced at the one choke point no fallback can bypass, and "a separate knowledge base per school" later is a policy line plus a placement switch, not a rewrite. |
| D14 | **Lesson plans are private by default; a teacher can opt in to share one with their school** (read and copy, never edit). Same-school check uses the signed `school_id`. Students never see lesson plans. | Teachers keep drafts private, colleagues can reuse good work, and no edit conflicts arise. Specific-teacher sharing or co-editing later is a new value, not a redesign (§17). |

**Not now:** a user directory or passwords in LessonForge, OAuth
authorization-server features, school-admin self-service uploads (§16 has the hook), Postgres row-level security,
rate limiting (§12 has the plan), assignments/rosters, Edboox→LF webhooks.

## 2. System view

```
┌──────────────────────── Edboox (system of record for users) ─────────────────────────┐
│ teachers · students · schools · teaching assignments                                 │
│ login ─► mint LF token (private key)          GET /.well-known/jwks.json (public)    │
│ [later] Edboox web UI ─────────────┐          GET /api/v1/ai/users/{sub}  (context)  │
└──────────────┬─────────────────────┼──────────────────▲──────────▲───────────────────┘
   (a) launch: auto-POST token       │ (b) Bearer token │ keys     │ context pull
               │                     │                  │ (cached) │ (API key, read-only,
               ▼                     ▼                  │          │  cached ~24 h)
┌──────────────────────── LessonForge (AI service) ───┼──────────┼───────────────────┐
│ our UI (static shells) ──► /api/v1 ◄── Edboox UI    │          │                   │
│                              │                       │          │                   │
│ auth: verify ───────────────────────────────────────┘          │                   │
│   └► principal (iss, sub) + local grants ─► permissions         │                   │
│ context: UserContextProvider ────────────────────────────────────┘                   │
│   └► UserContext ─► ScopePolicy ─► retrieval filters · generation defaults           │
│ routers: require(perm) ─► services(principal, context) ─► owner-scoped stores        │
│                                                                                      │
│ Postgres (lessonforge DB)        Qdrant (vectors + metadata)        LLM / embedder   │
└──────────────────────────────────────────────────────────────────────────────────────┘
```

Neither side reads the other's database. The integration surface is **four
contracts**: the token (§3), our API (§4), Edboox's user-context endpoint (§6),
and our activity feed and reports (§15).

## 3. Contract 1: the token (Edboox → LessonForge)

A JWT signed by Edboox with a key published at its JWKS URL. It carries
**identity plus only what authorization or the hot path needs.**

```jsonc
// header
{ "alg": "RS256", "kid": "edboox-2026-10", "typ": "JWT" }
// payload
{
  "iss": "https://edboox.example",      // must match a configured issuer
  "aud": "lessonforge",                 // tokens for other apps are rejected
  "sub": "student:42",                  // GLOBALLY unique within the issuer (see note)
  "iat": 1760430000, "exp": 1760430900, // API tokens ≤ 15 min; launch tokens ≤ 2 min
  "jti": "b7c1…",                       // unique; single-use for launch tokens
  "role": "student",                    // "student" | "teacher" (mapped by config)
  "name": "Sita Sharma",                // display only, never sent to the LLM
  "school_id": "sch-7",                 // tenant
  "grade": 8                            // students: lets scoping work even before a context pull
}
```

- **`sub` must be namespaced.** Edboox keeps students and teachers in separate
  tables, so a bare `42` is ambiguous. Edboox sends `student:42` / `teacher:42`; we
  key identity on `(iss, sub)` and map it to our own UUID.
- Claim names are configurable per issuer (§10), so the real Edboox doesn't have to
  match the fake one's naming.
- If the real Edboox can't sign asymmetrically, the verifier also accepts per-issuer
  `HS256` with a secret from env. Use it only as a fallback, since that secret can
  mint tokens.

### Flows

**(a) Launch: our UI, today.**
1. The user clicks *AI Assistant* in Edboox.
2. Edboox mints a 60 s token and renders an auto-submitting `<form method=POST action=…/auth/launch>`. The token goes in the POST body, not the URL, so it doesn't leak into logs, history, or Referer.
3. LessonForge verifies the token and rejects a reused `jti`.
4. LessonForge upserts the principal and kicks off a context refresh (§6).
5. LessonForge sets `lf_session` (HS256 `{principal_id, exp}`; httpOnly, Secure, SameSite=Lax).
6. LessonForge returns a `303` to `/chat` for students or `/` for teachers.

This is the LTI 1.3 launch pattern that LMS tool integrations use.

**(b) Bearer: Edboox UI or backend, later.** The Edboox web app gets a 15-minute
token from its own backend (`POST /api/ai-token`) and calls `/api/v1/*` directly
with `Authorization: Bearer …`. It refreshes the token before expiry. Calling the
API from the browser keeps chat streaming end-to-end without an Edboox proxy.
CORS allows Edboox's web origin.

**(c) Superadmin (us).** We log in through Edboox like anyone else; elevation is local:

```bash
lessonforge-admin grant  --issuer edboox --subject teacher:1 --role superadmin
lessonforge-admin revoke --issuer edboox --subject teacher:1 --role superadmin
lessonforge-admin grants
```

There is no HTTP endpoint that grants roles; the CLI needs DB access, which is the
boundary. Every grant and revoke goes to `audit_log`.

## 4. Contract 2: our API, so a UI migration is a frontend-only job

Moving from our UI to Edboox's UI breaks only when our UI relies on something
undocumented. These rules prevent that:

1. **Versioned prefix.** All JSON and streaming endpoints live under `/api/v1/…`.
   Page routes (`/`, `/chat`, `/units`, `/library`, `/corpus`) serve **static
   shells only**: they never embed data or call anything outside `/api/v1`. This also
   fixes today's overlap between the `/chat` page and the `/chat/...` API.
   `app.js` gets one `API_BASE` constant.
2. **No UI-only endpoints.** Whatever our pages need, Edboox's UI can use too:
   `GET /api/v1/me` (name, roles, permissions, context summary) drives navigation,
   and `/chat/config`, `/artifacts/manifest`, and `/artifacts/question-types`
   become documented, stable endpoints.
3. **Contract in CI.** `docs/openapi.v1.json` is checked in. A test regenerates it
   and fails on any **breaking** diff (removed path or field, narrowed type, new
   required input). Additive changes stay in v1; breaking ones need `/api/v2`, with
   v1 running alongside it for a deprecation window. Edboox can generate a typed
   client from this file.
4. **Streaming contract.** Chat replies stream as SSE over `fetch` (not
   `EventSource`, which can't send `Authorization`). Event names and payloads
   (`status`, `token`, `intent`, `artifact`, `awaiting`, `done`, `error`) get
   Pydantic models in `services/chat/events.py` and a documented schema in
   `docs/CHAT_EVENTS.md`. New event types are additive, and clients ignore
   unknown ones.
5. **Uniform errors.** Every non-2xx response is
   `{"error": {"code": "perm.denied", "message": "…", "details": …}}` with
   stable machine codes: `auth.missing`, `auth.expired`, `auth.invalid`,
   `perm.denied`, `not_found`, `validation`, `upstream.unavailable`. UIs branch on
   `code`, never on text.
6. **Stable shapes:** opaque string IDs (UUIDs), ISO-8601 UTC timestamps, and
   cursor pagination (`?limit=&cursor=` → `{items, next_cursor}`) on every list
   endpoint from day one. Adding pagination later would be a breaking change.
7. **Auth-agnostic.** The role-matrix test suite runs once with Bearer and once
   with the cookie, proving both clients see identical behaviour.

After migration our pages can stay as the owners' admin console (corpus
ingestion, ops), which Edboox has no reason to rebuild.

## 5. Authorization

### Permissions (in code)

| Permission | Covers (all under `/api/v1`) |
|------------|--------|
| `chat` | `/chat/*`: conversations, messages, chat artifacts, quiz attempts |
| `author` | `/lessons/*`, `/units/*`, `/artifacts/*`, `/documents/*`, `/export/*`, `/profile`, `/grounding/preview` |
| `corpus.manage` | `/corpus/*`: overview, records, preview, ingest, delete |
| `ops` | `/health/providers`, admin read endpoints |
| `reports.read` | `/reports/*`, `/events` (§15), limited to the caller's tenant unless the caller is `service` or `superadmin` |

`/health` stays public. `/me` requires only authentication.

### Roles → permissions (config)

| Role | Source | Permissions |
|------|--------|-------------|
| `student` | Edboox claim | `chat` |
| `teacher` | Edboox claim | `chat`, `author` |
| `service` | Edboox claim on a machine token (`sub: service:edboox`) | `reports.read` |
| `superadmin` | **local grant only** | `*` |

Effective roles are the mapped issuer role plus local grants. **Startup fails** if
`role_map` maps anything to `superadmin`, or if a role names an unknown permission.
An unmapped role at runtime gets 403; there is never a fallback role.

### Enforcement: once per surface

`api/main.py` (1356 lines) is split into area routers, each declaring its
permission once:

```python
lessons = APIRouter(prefix="/api/v1/lessons", tags=["lessons"],
                    dependencies=[Depends(require(Perm.AUTHOR))])
```

Routes that touch owned data take `p: Principal = Depends(current_principal)` and
pass it down. **Stores scope every query by owner**
(`WHERE id = %s AND owner_id = %s`). A miss returns 404. Chat artifacts and
attempts are authorized through their conversation's owner.

**Chat skills.** `SkillSpec` gains `requires: Perm = Perm.CHAT`. The intent router
builds its catalogue from the skills the principal may use, so a teacher-only skill
is never offered to a student, not merely blocked after the fact.

## 6. Contract 3: user context (Edboox data that makes the AI better)

### What we ask Edboox for (data minimisation: these are minors)

| Who | Fields | Used for |
|-----|--------|----------|
| Student | grade, section, school (id, name, district, province), medium of instruction (`en`/`ne`), stream/faculty (grades 11–12), optional subjects, preferred language | retrieval scope, answer language, local relevance, which books apply |
| Teacher | school (as above), **teaching assignments** `[{grade, section, subject, class_id}]`, preferred language | lesson/unit defaults, grade/subject pickers, future assignments |
| Never | DOB, address, phone, guardians, photos, marks/attendance | not needed. Marks would only come later, behind explicit consent, as their own feature |

### Edboox endpoint (fake now, real later)

```
GET /api/v1/ai/users/{sub}            X-Api-Key: <LF's read-only key>     (server → server)
200 {
  "sub": "student:42", "role": "student",
  "school": { "id": "sch-7", "name": "Shree Janata Secondary School",
              "district": "Kaski", "province": "Gandaki" },
  "student": { "grade": "Class 8", "section": "A", "medium": "nepali",
               "stream": null, "optional_subjects": ["Computer Science"] },
  "teacher": null,
  "preferred_language": "ne",
  "updated_at": "2026-10-14T09:12:00Z"
}
```

The key is read-only and limited to this projection endpoint. The adapter hides
how it authenticates, so moving to OAuth client credentials later is an adapter
change only.

### Our side: port, adapter, mapper

```python
class UserContext(BaseModel):            # OUR model, in OUR vocabulary
    school: SchoolRef | None             # id, name, district, province
    language: str | None                 # "en" | "ne"
    student: StudentContext | None       # grade:int, section, medium, stream, optional_subjects
    teacher: TeacherContext | None       # assignments: list[TeachingAssignment(grade:int, section, subject, class_id)]
    synced_at: datetime | None

class UserContextProvider(ABC):          # port; config picks the adapter
    def fetch(self, principal: Principal) -> UserContext | None: ...
# adapters: "claims" (from the token only; zero dependencies) · "edboox_http"
```

- **Anti-corruption mapper.** The Edboox adapter turns Edboox's shapes into ours,
  for example `"Class 8"` → `8` and `"Science and Technology"` → `"Science"`,
  through the shared vocabulary (D9). Nothing past the adapter ever sees Edboox's
  raw format, so Edboox renaming a field touches one file.
- **Caching and refresh.** The context is stored on the principal (`context jsonb`,
  `context_synced_at`). It refreshes on launch, and in the background on any request
  where it's older than `context.ttl_h`. Requests never wait on Edboox.
- **Fails soft.** If Edboox is down, we keep the last-known context and fall back
  to token claims. Context drives *personalisation only*; **no access decision
  ever reads it** (D8), so stale context can't grant anything.
- **Privacy.** Only what a prompt needs reaches the LLM (grade, language, district
  for local examples). Names and IDs never do. Deleting a principal cascades.

### How context becomes retrieval filters: `ScopePolicy`

The vector store already filters on payload metadata (`grade`, `subject`, `class`,
plus free-form `tags`, see `ChatDefaults.filters`). `ScopePolicy` turns context
into those filters from config, so linking a new Edboox field to a new corpus
metadata key is a YAML change:

```yaml
scope:
  bindings:                  # UserContext path → vector metadata key
    student.grade: grade
    student.stream: stream   # matches once the corpus is tagged with `stream`
  roles:
    student: { mode: default }   # default | locked
    teacher: { mode: off }       # teachers pick their own scope
```

| mode | Behaviour |
|------|-----------|
| `off` | context is not applied to retrieval (the user's chosen filters only) |
| `default` | context **pre-fills** a new conversation's `ChatDefaults`; the user may change it; today's lenient fallback to broad search still applies. **Recommended for students now.** |
| `locked` | filters are applied **server-side on every turn**; client-supplied scope for bound keys is ignored; no broad fallback (the answer says the topic is outside the student's materials). Use it if schools want students kept to their own grade. |

For teachers, `assignments` fill grade/subject pickers and `TeacherProfile`
defaults (the existing `apply_defaults` path). An explicit request still wins.

### Shared vocabulary (D9)

`src/lessonforge/domain/vocabulary.py` holds the canonical grades (`int` 1–12)
and subjects, with aliases loaded from `config/vocabulary.yaml`:

```yaml
subjects:
  Science:     [science, "science and technology", "sci", "विज्ञान", "विज्ञान तथा प्रविधि"]
  Mathematics: [maths, math, "गणित"]
grade_patterns: ['^(?:class|grade)?\s*(\d{1,2})$', '^(I|II|III|…|XII)$']
```

The Edboox mapper **and** corpus ingestion (`/corpus/ingest`, the CLI) both
normalise through it. Ingestion rejects unknown subjects with a 422 rather than
storing an unfilterable value. Adding a subject is a YAML change.

## 7. Postgres schema (LessonForge DB)

Conventions: `uuid` PKs, `timestamptz`, `jsonb` for nested or free-form parts
(domain models serialise into it unchanged), FKs with `ON DELETE CASCADE` where
a child can't outlive its parent, and `CHECK` for small enums.

```sql
-- ── identity, access, context ───────────────────────────────────────────────
CREATE TABLE principals (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  issuer            text NOT NULL,               -- config issuer name: 'edboox'
  subject           text NOT NULL,               -- token 'sub': 'student:42'
  external_role     text NOT NULL,               -- mapped role from the latest token
  display_name      text,
  tenant_id         text,                        -- Edboox school; stored, not yet enforced
  context           jsonb NOT NULL DEFAULT '{}', -- UserContext (our vocabulary)
  context_synced_at timestamptz,
  disabled_at       timestamptz,                 -- local kill switch → 403
  created_at        timestamptz NOT NULL DEFAULT now(),
  last_seen_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (issuer, subject)
);

CREATE TABLE role_grants (                       -- LOCAL elevation only (D5)
  principal_id uuid NOT NULL REFERENCES principals(id) ON DELETE CASCADE,
  role         text NOT NULL CHECK (role IN ('superadmin')),
  granted_by   text NOT NULL,                    -- operator running the CLI
  granted_at   timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (principal_id, role)
);

CREATE TABLE used_token_ids (                    -- launch-token replay guard
  issuer     text NOT NULL,
  jti        text NOT NULL,
  expires_at timestamptz NOT NULL,               -- purged after this
  PRIMARY KEY (issuer, jti)
);

CREATE TABLE knowledge_spaces (                 -- §16
  id         text PRIMARY KEY,                    -- 'global' | 'school:sch-7'
  kind       text NOT NULL CHECK (kind IN ('global','school')),
  tenant_id  text,                                -- NULL for global
  name       text NOT NULL,
  placement  text,                                -- NULL = vector_store default; 'dedicated' later
  status     text NOT NULL DEFAULT 'active' CHECK (status IN ('active','archived')),
  created_at timestamptz NOT NULL DEFAULT now(),
  CHECK ((kind = 'global') = (tenant_id IS NULL))
);

CREATE TABLE corpus_sources (                   -- one row per ingested document
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  space_id     text NOT NULL REFERENCES knowledge_spaces(id),
  collection   text NOT NULL,                   -- reference | curriculum | …
  doc_id       text NOT NULL,                   -- the doc_id stamped on its chunks
  title        text NOT NULL,
  source_label text,
  checksum     text NOT NULL,                   -- sha256 of the original upload
  chunks       int  NOT NULL,
  ingested_by  uuid REFERENCES principals(id) ON DELETE SET NULL,
  ingested_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (space_id, collection, doc_id)
);

CREATE TABLE audit_log (                         -- privileged actions only
  id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  principal_id uuid REFERENCES principals(id) ON DELETE SET NULL,
  actor        text NOT NULL,
  action       text NOT NULL,                    -- 'corpus.ingest', 'grant.add', …
  target       text,
  detail       jsonb NOT NULL DEFAULT '{}',
  at           timestamptz NOT NULL DEFAULT now()
);

-- ── teacher data ────────────────────────────────────────────────────────────
CREATE TABLE teacher_profiles (
  principal_id uuid PRIMARY KEY REFERENCES principals(id) ON DELETE CASCADE,
  data         jsonb NOT NULL,                   -- TeacherProfile
  updated_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE documents (
  id              uuid PRIMARY KEY,
  owner_id        uuid NOT NULL REFERENCES principals(id) ON DELETE CASCADE,
  tenant_id       text,
  kind            text NOT NULL CHECK (kind IN ('lesson','unit')),
  title           text NOT NULL,
  grade           int,
  subject         text,
  head_version_id uuid,
  visibility      text NOT NULL DEFAULT 'private'
                  CHECK (visibility IN ('private','school')),   -- §17
  shared_at       timestamptz,                   -- set when visibility becomes 'school'
  copied_from     uuid REFERENCES documents(id) ON DELETE SET NULL,  -- provenance of a copy
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),
  CHECK (visibility = 'private' OR tenant_id IS NOT NULL)        -- can't share without a school
);
CREATE INDEX ix_documents_owner ON documents (owner_id, updated_at DESC);
CREATE INDEX ix_documents_school_shared ON documents (tenant_id, shared_at DESC)
  WHERE visibility = 'school';

CREATE TABLE document_versions (                 -- today's columns, typed
  id                uuid PRIMARY KEY,
  document_id       uuid NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  ord               int  NOT NULL,
  parent_id         uuid REFERENCES document_versions(id),
  origin            text NOT NULL,
  snapshot          jsonb NOT NULL,
  instruction       text NOT NULL DEFAULT '',
  diff              jsonb NOT NULL,
  grounding_sources jsonb NOT NULL DEFAULT '[]',
  scores            jsonb NOT NULL DEFAULT '{}',
  created_at        timestamptz NOT NULL DEFAULT now(),
  UNIQUE (document_id, ord)
);

-- ── chat ────────────────────────────────────────────────────────────────────
CREATE TABLE chat_conversations (
  id         uuid PRIMARY KEY,
  owner_id   uuid NOT NULL REFERENCES principals(id) ON DELETE CASCADE,
  title      text NOT NULL,
  defaults   jsonb NOT NULL,                     -- ChatDefaults (pre-filled by ScopePolicy)
  summary    text NOT NULL DEFAULT '',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_chat_conv_owner ON chat_conversations (owner_id, updated_at DESC);

CREATE TABLE chat_messages (
  id              uuid PRIMARY KEY,
  conversation_id uuid NOT NULL REFERENCES chat_conversations(id) ON DELETE CASCADE,
  ord             int  NOT NULL,
  role            text NOT NULL CHECK (role IN ('user','assistant','system')),
  content         text NOT NULL,
  citations       jsonb NOT NULL DEFAULT '[]',
  meta            jsonb NOT NULL DEFAULT '{}',
  created_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (conversation_id, ord)
);

CREATE TABLE chat_artifacts (
  id              uuid PRIMARY KEY,
  conversation_id uuid NOT NULL REFERENCES chat_conversations(id) ON DELETE CASCADE,
  message_id      uuid REFERENCES chat_messages(id) ON DELETE SET NULL,
  kind            text NOT NULL,
  title           text NOT NULL,
  payload         jsonb NOT NULL,
  meta            jsonb NOT NULL DEFAULT '{}',
  created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE chat_attempts (
  id              uuid PRIMARY KEY,
  artifact_id     uuid NOT NULL REFERENCES chat_artifacts(id) ON DELETE CASCADE,
  conversation_id uuid NOT NULL REFERENCES chat_conversations(id) ON DELETE CASCADE,
  owner_id        uuid NOT NULL REFERENCES principals(id) ON DELETE CASCADE,
  kind            text NOT NULL,
  status          text NOT NULL,
  score           real NOT NULL,
  total           int  NOT NULL,
  state           jsonb NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now()
);
```

- **Forward-looking columns:** `chat_attempts.owner_id` (equals the conversation
  owner today; differs once teachers assign quizzes), `tenant_id`, and
  `principals.context`. Each costs one column now and saves a migration later.
- **Migrations:**
  - Numbered plain-SQL files in `src/lessonforge/db/migrations/`, applied by
    `lessonforge-db migrate`, which records them in `schema_migrations`. The code is
    raw `psycopg` with no ORM, so Alembic would add SQLAlchemy just to run SQL.
  - The app refuses to start if the schema is behind; production never auto-migrates.
    Dev compose runs `migrate` before `api`.
  - `0002_import_legacy.sql` maps `owner_id='default'` rows to a `('local','default')`
    principal, so dev data survives.
- **Pool:** one `Database` (`psycopg_pool`) in the `Container`, injected into every
  Postgres store and the principal directory. It replaces connect-per-operation and
  the two DSNs.

## 8. Code layout

```
src/lessonforge/
  auth/
    principal.py    Principal (frozen), Perm enum
    policy.py       roles → perms from config + startup validation
    verifier.py     TokenVerifier port; JwtVerifier (PyJWT + PyJWKClient, per-issuer alg allowlist)
    directory.py    PrincipalDirectory port; Postgres + Memory impls (upsert (iss,sub), grants)
    session.py      /auth/launch, cookie issue/read, /auth/logout
    deps.py         current_principal (Bearer → cookie → 401), require(perm) → 403
  context/
    model.py        UserContext & friends
    provider.py     UserContextProvider port + "claims" adapter + registry
    scope.py        ScopePolicy: context → ChatDefaults / filters / profile defaults
  integrations/
    edboox.py       "edboox_http" UserContextProvider + Edboox→UserContext mapper
  domain/vocabulary.py   canonical grades/subjects + aliases (config/vocabulary.yaml)
  db/               pool.py, migrate.py (+ `lessonforge-db`), migrations/*.sql
  admin/cli.py      `lessonforge-admin grant|revoke|grants|sync-context <sub>`
  api/errors.py     error envelope + exception handlers
  api/routers/      chat, lessons, units, artifacts, documents, corpus, export, me, ops, pages
dev/fake_edboox/    separate service; must not import lessonforge (§9)
docs/openapi.v1.json, docs/CHAT_EVENTS.md
```

New dependencies: `PyJWT[crypto]` and `psycopg_pool`.

## 9. Fake Edboox (dev only)

A small FastAPI app in `dev/fake_edboox/`, run as its own compose service on port
8200 under `profiles: [dev]`. It has its **own database** (`edboox`) and its own
DB user in the same Postgres container, created by an init script. It uses
Edboox-style shapes on purpose (`"Class 8"`, long subject names), so our mapper is
exercised instead of bypassed.

```sql
CREATE TABLE schools  (id text PRIMARY KEY, name text NOT NULL,
                       district text, province text);
CREATE TABLE teachers (id serial PRIMARY KEY, school_id text REFERENCES schools(id),
                       username text UNIQUE NOT NULL, password_hash text NOT NULL,
                       full_name text NOT NULL, preferred_language text DEFAULT 'en');
CREATE TABLE students (id serial PRIMARY KEY, school_id text REFERENCES schools(id),
                       username text UNIQUE NOT NULL, password_hash text NOT NULL,
                       full_name text NOT NULL, grade text NOT NULL,   -- 'Class 8'
                       section text, medium text, stream text,
                       optional_subjects text[] NOT NULL DEFAULT '{}',
                       preferred_language text DEFAULT 'ne');
CREATE TABLE teaching_assignments (
  teacher_id int REFERENCES teachers(id) ON DELETE CASCADE,
  grade text NOT NULL, section text, subject text NOT NULL,     -- 'Science and Technology'
  PRIMARY KEY (teacher_id, grade, section, subject));
```

Seed data: one school (Kaski, Gandaki), the owner account `teacher:1` (granted
superadmin by the dev seed through the CLI), two teachers with assignments, and
three students across grades 8 and 10.

| Endpoint | Purpose |
|----------|---------|
| `GET/POST /login` | username + password → Edboox session (hashed passwords even in the fake) |
| `GET /` | dashboard with an **Open AI assistant** button |
| `POST /launch/lessonforge` | mints a 60 s token and renders the auto-POST form (§3a) |
| `POST /api/ai-token` | 15-minute bearer token for the logged-in user (§3b; for the future Edboox UI) |
| `GET /.well-known/jwks.json` | public keys; RSA key pair generated at first boot and kept on a volume |
| `GET /api/v1/ai/users/{sub}` | the user-context projection (§6), `X-Api-Key` required |

Switching to the real Edboox means setting `EDBOOX_ISSUER`, `EDBOOX_JWKS_URL`,
`EDBOOX_LOGIN_URL`, `EDBOOX_WEB_ORIGIN`, `EDBOOX_API_URL`, and `EDBOOX_API_KEY`.
Claim names and mapper aliases change only if Edboox's naming differs.

## 10. Configuration

```yaml
database:
  url: ${DATABASE_URL}                # the only DSN (replaces CHAT_DB_URL / DOCS_DB_URL)
  pool_max: 10

api:
  cors_origins: ["${EDBOOX_WEB_ORIGIN:-http://localhost:8200}"]

auth:
  issuers:                            # a second integrating app = one more entry
    - name: edboox
      issuer: ${EDBOOX_ISSUER:-http://localhost:8200}
      jwks_url: ${EDBOOX_JWKS_URL:-http://localhost:8200/.well-known/jwks.json}
      audience: lessonforge
      algorithms: [RS256]             # allowlist; never trust the token's own 'alg'
      claims: { role: role, tenant: school_id, name: name, grade: grade }
      role_map: { student: student, teacher: teacher, service: service }
  leeway_s: 30
  max_token_age_s: 900
  login_url: ${EDBOOX_LOGIN_URL:-http://localhost:8200/login}
  session:
    cookie: lf_session
    ttl_min: 480
    secret: ${LF_SESSION_SECRET}      # env only; startup fails if empty

rbac:
  roles:
    student:    [chat]
    teacher:    [chat, author]
    service:    [reports.read]
    superadmin: ["*"]

knowledge:                            # §16: who reads which spaces ({tenant} = token school_id)
  read:
    student:    [global, "school:{tenant}"]
    teacher:    [global, "school:{tenant}"]
    superadmin: ["*"]
  # Future "separate knowledge base per school": drop `global` from the lists above.

activity:
  provider: postgres                  # ← swap: postgres | noop
  retention_days: 400                 # raw events; a nightly job deletes older rows

context:
  provider: edboox_http               # ← swap: claims | edboox_http
  base_url: ${EDBOOX_API_URL:-http://localhost:8200}
  api_key: ${EDBOOX_API_KEY}          # env only
  ttl_h: 24
  timeout_s: 3

scope:                                # see §6
  bindings: { student.grade: grade }
  roles:
    student: { mode: default }        # default | locked | off
    teacher: { mode: off }
```

`chat.store`, `documents`, and `profile` keep `provider: postgres | memory`; their
`url`/`path` keys go away.

## 11. Testing

- `conftest.py` overrides `current_principal` with a superadmin and `context` with
  the `claims` provider by default, so the roughly 80 existing API tests run as-is
  apart from the `/api/v1` path move. There is **no auth-off switch** in production code.
- **Verifier:** generated RSA keys plus a `respx` JWKS mock. Cases: wrong `aud`/`iss`,
  expired, too old, `alg` switched to `none`/HS256, unknown `kid`, reused launch `jti`.
- **Role matrix:** every route × role × {bearer, cookie}, generated from the router
  table so it can't drift.
- **Corpus isolation:** a School-B principal retrieves School-A's private chunk
  through **no** path (chat, lesson, unit, quiz, section/chapter expansion,
  table of contents, empty-result fallback, corpus listing). Every path is in one
  parametrised test, and `global` stays visible to both.
- **IDOR:** teacher B gets 404 on A's **private** document; a student gets 404 on
  another student's conversation.
- **Lesson sharing (§17):**
  - same-school teacher can read and copy a `school` plan, but gets 403 on edit, refine, undo, redo, delete, or re-share;
  - another school's teacher gets 404 on it;
  - a student gets 403 on all document routes;
  - un-sharing removes it from colleagues' list immediately;
  - a copy is independent of the original in both directions.
- **Context:** mapper fixtures (`"Class 8"` → 8, subject aliases, missing fields);
  Edboox down → last-known context kept; a stale context never changes a permission.
- **Scope:** `default` pre-fills and can be overridden; `locked` ignores client scope
  and skips the broad fallback.
- **Contract:** the OpenAPI snapshot breaking-change diff; the error envelope on
  401/403/404/422.
- **Integration (`RUN_INTEGRATION=1`):** stores against real Postgres; a launch
  round-trip against the fake Edboox.

## 12. Security checklist

- Signature verified via JWKS; algorithm allowlist; `iss`, `aud`, `exp`, `nbf`, `iat`
  required; `max_token_age_s`; 30 s leeway. An unknown `kid` triggers one
  rate-limited JWKS refetch.
- Launch tokens are single-use (`used_token_ids`) and expire in 60 s; they travel in
  a POST body, never a URL.
- Superadmin comes only from local CLI grants, which are audited; config validation
  forbids mapping it from a claim.
- Fail closed: unknown issuer or missing claim → 401; unmapped role, disabled
  principal, or missing permission → 403.
- Owner comes from the principal and is scoped in SQL; a foreign resource → 404.
  Shared lesson plans widen **read only**: `owner = me OR (visibility = 'school'
  AND tenant_id = <signed school_id>)`. Every write path still requires `owner = me`.
- **Corpus isolation (§16):** every vector-store read takes a required `AccessScope`
  that is ANDed in by the adapter, separately from the metadata `where`. The
  lenient "broaden to unfiltered" fallback drops `where`, never the scope. The
  scope comes from the **signed** `school_id` claim, never from cached context or
  the request body. Chunk and document IDs include the space, so identical
  uploads in two schools never overwrite each other.
- **Authorization never reads cached context.** Context only personalises.
- Cookie: httpOnly, Secure, SameSite=Lax, holding only `principal_id`; permissions
  are resolved server-side and cached about 60 s, so revocations land quickly.
- CSRF: cookie-authenticated mutations require an `X-LF-Request: 1` header, added in
  one place in `app.js`. Bearer requests are not exposed to CSRF.
- CORS: explicit origin list; never `*` together with credentials.
- Secrets live only in env; `Authorization`, cookies, and `X-Api-Key` are redacted
  from logs.
- Prompts get minimal user context: no names or IDs.
- In production the app DB role has DML only; migrations run under a separate DDL role.
- **Next step: rate limits.** Students get chat freely, and each turn costs LLM
  calls. A Postgres-backed token bucket per `(principal, perm)` fits without new
  infrastructure.

## 13. Delivery plan (each phase ships alone, tests green)

1. **Postgres foundation:** pool, migration runner, typed schema, chat/document/
   profile stores moved to it, sqlite retired, single `DATABASE_URL`. No behaviour change.
2. **API contract:** routers under `/api/v1`, error envelope, pagination, SSE event
   models, OpenAPI snapshot test, and `API_BASE` in the frontend. No auth yet, so a
   pure refactor.
3. **Auth core and enforcement:** principal, permissions, policy, verifier, directory,
   `require`, `/me`, `owner_id` params removed, owner-scoped stores,
   `SkillSpec.requires`, role-matrix and IDOR tests.
4. **Fake Edboox and launch:** dev compose profile, launch/session/logout, `401 → login_url`
   in the frontend, CSRF header, navigation driven by `/me`.
5. **User context:** vocabulary (and ingest normalisation), `UserContext`, `claims`
   and `edboox_http` providers, ScopePolicy wired into conversation creation and
   lesson defaults.
6. **Admin and audit:** `lessonforge-admin`, audit on corpus routes, purge of
   `used_token_ids`.
3b. **Knowledge spaces, with phase 3, before any second school goes live:**
   `AccessScope`, the scoped `VectorStore` signature, `_space` payload index,
   backfill of existing points to `global`, space-aware ingest/IDs,
   `knowledge_spaces` / `corpus_sources`, corpus UI space picker, isolation test.
3c. **Lesson sharing, after phase 3 (needs owner-scoped stores):** `visibility`
   / `shared_at` / `copied_from` columns, share/unshare/copy endpoints, the
   "Shared in my school" list, library UI toggle and tab, sharing tests.
7. **Activity and reporting:** `activity_events`, the event catalogue, emission
   points, LLM token usage, `/api/v1/events` feed, `/api/v1/reports/*`, retention
   job, and the fake Edboox pulling the feed into a simple usage page.

## 14. Open questions

1. ~~UI ownership~~. **Answered:** Edboox will build its own UI on our API; ours is
   used until then (§4).
2. **Real Edboox signing:** can it sign RS256 and publish JWKS? (The HS256 fallback
   exists if not.)
3. **Student scope:** should students start at `default` (pre-filled, can browse
   other grades) or `locked` (own grade only)? This is a one-line config change
   either way.
4. **Student-visible corpora:** if `exemplar`/`pedagogical` hold teacher-only
   material (answer keys, model plans), add `collections` to `scope.roles.student`
   so students search only `reference`/`curriculum`.
5. **Retention:** chat logs of minors. Agree on a retention window and a
   delete-on-request path with Edboox; the `ON DELETE CASCADE` from `principals`
   makes deleting a user one statement.
6. **Who may read transcripts:** activity is content-free by design. If schools
   need to read student conversations (safeguarding), that is a separate,
   audited endpoint and a policy decision, not part of analytics.
7. **Who uploads school-private documents** once it isn't only us: a
   `school_admin` role with `corpus.manage` limited to its own space (§16)?
8. **Edboox's real vocabulary:** get their exact grade/subject/stream values so the
   alias table matches on day one.

## 15. Activity tracking and reporting (Contract 4: LessonForge → Edboox)

**Goal:** Edboox can see who used what, how much, what kind of help was asked
for, how quizzes went, and what it costs, per school, grade, role, and user,
**without our storing or exposing what children typed.**

### Three kinds of log, kept apart

| Kind | Purpose | Where | Who reads it |
|------|---------|-------|--------------|
| **Activity events** | product usage and monitoring (this section) | `activity_events` table | Edboox (feed and reports), us |
| **Audit log** | security-relevant privileged actions (grants, ingestion) | `audit_log` (§7) | us only |
| **Operational logs** | debugging, errors, latency | structured JSON on stdout with a `request_id` | us only |

Mixing them leads either to leaking debug detail to a partner or to losing audit
rows among millions of chat events.

### The event

```sql
CREATE TABLE activity_events (
  id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,  -- also the feed cursor
  occurred_at   timestamptz NOT NULL DEFAULT now(),
  type          text NOT NULL,           -- 'chat.message', 'quiz.attempt.finished', …
  schema_ver    smallint NOT NULL DEFAULT 1,
  principal_id  uuid REFERENCES principals(id) ON DELETE SET NULL,
  subject       text,                    -- Edboox 'sub' (student:42), denormalised so
                                         -- Edboox can join to its users directly
  role          text NOT NULL,           -- student | teacher | superadmin | service
  tenant_id     text,                    -- school
  grade         int,                     -- from UserContext at the time of the event
  resource_type text,                    -- conversation | quiz | document | …
  resource_id   uuid,
  props         jsonb NOT NULL DEFAULT '{}',  -- type-specific, schema-validated (below)
  request_id    text                     -- ties to operational logs
);
CREATE INDEX ix_activity_tenant_time ON activity_events (tenant_id, occurred_at);
CREATE INDEX ix_activity_type_time   ON activity_events (type, occurred_at);
CREATE INDEX ix_activity_subject     ON activity_events (subject, occurred_at);
```

One table, append-only. When volume grows, partition it by month; that is a
migration, not a redesign. Deleting a user anonymises their events: the delete
path sets `principal_id` and `subject` to NULL, so school-level counts stay correct
but nothing points back to the child.

### Event catalogue (versioned, documented in `docs/ACTIVITY_EVENTS.md`)

Every `type` has a **Pydantic props model**. Unknown props are rejected, so
free text or names can't be logged by accident. The model is the privacy guarantee.

| type | When | props (no free text) |
|------|------|----------------------|
| `session.started` | launch or first bearer call of the day | `channel` (launch / api) |
| `chat.conversation.created` | new conversation | `scope_mode`, `grade`, `subject` |
| `chat.message` | each answered turn | `intent` (qa / quiz / …), `intent_source`, `confidence`, `subject`, `chapters` (from citation metadata), `collections`, `citations`, `grounded` (book hit or fallback), `latency_ms`, `tokens_in`, `tokens_out`, `model` |
| `quiz.created` | quiz made (chat skill or lesson/unit) | `source` (chat / lesson / unit), `questions`, `question_types`, `difficulty`, `subject`, `chapter` |
| `quiz.attempt.started` | student starts a quiz | `quiz_id` |
| `quiz.attempt.finished` | student finishes | `score`, `total`, `percent`, `duration_s`, `retry` |
| `lesson.generated` / `unit.generated` | teacher generates | `grade`, `subject`, `days`, `latency_ms`, `tokens_*` |
| `document.refined` | AI "Improve" / manual edit | `section`, `kind` (ai / manual) |
| `document.shared` / `document.unshared` | teacher changes visibility | `kind`, `grade`, `subject` |
| `document.copied` | colleague copies a shared plan | `kind`, `grade`, `subject`, `source_owner` (Edboox `sub`) |
| `artifact.exported` | download | `artifact`, `format` |
| `corpus.ingested` | superadmin ingests (also audited) | `collection`, `documents`, `chunks` |

**"What kind of chat" comes without content.** The intent router already
classifies every turn (`RoutedIntent`: skill, confidence, source), and citations
carry book metadata (chapter, heading path). So an event can say *"Grade 8 student,
Science, chapter 'Photosynthesis', plain question, answered from the textbook"*,
which covers what Edboox needs, without logging the message.

**Cost.** Ollama returns `prompt_eval_count` / `eval_count`. `LLMClient` exposes
them as a `usage` result, and the pipeline sums them per turn into `tokens_in`
and `tokens_out`. Edboox can see AI cost per school without our building billing.

### Emitting events: one port, a handful of call sites

```python
class ActivityRecorder(ABC):            # port; config: postgres | noop (tests: memory)
    def record(self, event: ActivityEvent) -> None: ...

recorder.record(QuizAttemptFinished(actor=p, resource_id=a.id,
                                    score=a.score, total=a.total, duration_s=…))
```

- `ActivityEvent.actor(p)` fills subject, role, tenant, and grade from the
  `Principal` and `UserContext` in one place. Call sites only add the props
  specific to their event.
- **Never breaks the request.** Writes run after the response, as FastAPI
  background tasks, or at the end of the chat stream. A failed write is logged
  and dropped. Losing one analytics row is acceptable; failing a student's
  answer is not.
- Emission lives in services, where the meaning is known (score, intent), not in
  HTTP middleware, which only sees URLs.

### How Edboox gets the data: pull, not push

**1. Event feed: raw, incremental, for Edboox's own warehouse or dashboards.**

```
GET /api/v1/events?after=<cursor>&limit=1000&types=quiz.attempt.finished,chat.message
Authorization: Bearer <service token>            → requires reports.read
200 { "items": [ {id, occurred_at, type, subject, role, tenant_id, grade,
                  resource_type, resource_id, props}, … ],
      "next_cursor": "184467" }
```

Edboox stores `next_cursor` and polls every few minutes. The cursor is the event
`id`, so the feed is **gap-free and replayable**: if Edboox misses a day, it
resumes from where it stopped. Webhooks would need delivery retries, signing,
and dead-letter handling on both sides, so they wait until there is a real-time
need.

**2. Summary reports: ready-made numbers, so Edboox doesn't have to crunch.**

| Endpoint | Answers |
|----------|---------|
| `GET /api/v1/reports/usage?from&to&group_by=day,school,role` | active users, chats, quizzes, lessons, tokens |
| `GET /api/v1/reports/chat-topics?from&to&school&grade` | top subjects/chapters asked about; share of turns answered from the book vs. fallback |
| `GET /api/v1/reports/quizzes?from&to&school&grade&subject` | quizzes made and taken, average score, completion rate, weakest chapters |
| `GET /api/v1/reports/users/{sub}/summary?from&to` | one user's activity: what Edboox shows on a student or teacher page |

These are plain `GROUP BY` queries over the indexed table. A nightly daily-rollup
table is added only when real data volume makes them slow.

**Who sees what.** `reports.read` is tenant-scoped by default:
- a principal with a `school_id` sees only that school;
- the Edboox `service` token sees all schools it issued;
- superadmin sees everything.

A future Edboox "school admin" role is one `role_map` line.

### Privacy and retention

- No message text, names, or free-form input in events, enforced by the props
  models and covered by a test that walks every event type.
- `subject` is Edboox's own pseudonymous ID; Edboox already knows who it is,
  and nobody else can resolve it.
- Raw events are deleted after `activity.retention_days`. Aggregated reports over
  older periods stay possible only through a rollup without `subject`.
- Reading actual conversations (safeguarding) is out of scope for analytics.
  See open question 6 in §14.

### Fake Edboox

It gets a `/usage` page that pulls `/api/v1/events` and `/api/v1/reports/usage`
with a service token. This proves the round trip end to end, as the real Edboox
would do it.

## 16. Knowledge spaces: shared curriculum plus school-private documents

### The rule today

- **Common curriculum and approved teaching resources** are visible to every school.
- **School A's private documents** are visible only to School A.
- **School B's private documents** are visible only to School B.

### The model: one concept, *space*

A **knowledge space** is a named shelf of documents: `global`, `school:sch-7`, and
so on. It is **orthogonal to the five collections**. A collection says *what kind*
of material something is (reference, curriculum, pedagogical, …); a space says
*who may see it*. School A's private notes on photosynthesis go in the `reference`
collection, `school:sch-7` space.

```
                 reference   curriculum   pedagogical   exemplar   local_context
 global           CDC books   NEB outcomes  misconceptions …
 school:sch-7     A's notes   —             A's tips      A's model lessons
 school:sch-9     B's notes   …
```

Every chunk carries the reserved payload key `_space`. Ingestion stamps it; no
caller can set it through record metadata.

### Reading: `AccessScope`, a value nobody can forget or drop

```python
@dataclass(frozen=True)
class AccessScope:
    spaces: tuple[str, ...]           # e.g. ("global", "school:sch-7")
    ALL: ClassVar[AccessScope]        # explicit, for the admin CLI / superadmin only

class KnowledgePolicy:                # config `knowledge.read`
    def scope_for(self, p: Principal) -> AccessScope: ...
        # "school:{tenant}" filled from the SIGNED school_id claim. No school
        # claim → global only. Never from cached context or the request.
```

- **One choke point.** Every retrieval path (chat, lesson grounding, units,
  quizzes, the section/chapter expansion `fetch`, table of contents, corpus
  listing, counts) ends in the `VectorStore` port. Its read methods gain a
  **required keyword argument** `scope: AccessScope` with no default. A call site
  that forgets it fails immediately in tests, not silently in production.
- **The adapter enforces it** as a separate filter condition
  (`_space ∈ scope.spaces`, Qdrant `MatchAny` on a keyword index), ANDed with
  whatever metadata `where` the caller built. The existing lenient fallback
  ("nothing matched, search unfiltered") removes `where`, **never the scope**.
  This is why the scope can't simply be another key in `where`.
- **Plumbing.** The services already need the principal (§5), so the request-scoped
  `RequestContext {principal, user_context, access_scope}` carries the scope down
  to `Retriever`, and every path inherits it. No global or thread-local state.
- **Ranking across shelves needs no change.** Results from `global` and the
  school's own space are one candidate pool, reranked together. A school's own
  material can be favoured with the existing `collection_weights` mechanism, plus
  an optional `space_weights: {own_school: 1.1}` if it proves useful.

### Writing: ingestion targets one space

- `POST /api/v1/corpus/ingest` takes `space` (default `global`). The policy checks
  the caller may write there. Today only superadmin can, to any space.
- **IDs include the space.** `content_id(...)` and the document `doc_id` hash the
  space in, so the same PDF uploaded by two schools makes two independent copies,
  and School B's re-upload can't overwrite School A's chunks.
- Each ingested document gets a `corpus_sources` row (space, collection, doc_id,
  title, checksum, chunks, who, when). Corpus management can then list "School A's
  documents", delete one cleanly, or re-index a space (needed when the embedder
  changes) without scanning the vector store.
- `knowledge_spaces` holds the shelves. A school's space is created on its first
  upload. Archiving a school is `status = 'archived'` plus a delete-by-`_space`
  in the vector store: one operation, because the space is a single indexed key.

### Later: "a separate knowledge base per school" stays a small change

That phrase can mean two different things, and both are already covered.

**1. Logical separation: a school sees only its own shelf.** Change the policy:

```yaml
knowledge:
  read:
    student: ["school:{tenant}"]           # no global
    teacher: ["school:{tenant}"]
```

Per-school exceptions (for example "School C also sees `global`") become a
`read_overrides` map. No code changes.

**2. Physical separation: a school's vectors in their own collection** (strict
isolation, a very large school, data residency). Placement is a **vector-store
adapter concern**, invisible above the `VectorStore` port:

| `vector_store.placement` | Physical layout | Search |
|---|---|---|
| `shared` (**now**) | `lf_reference` holds all spaces; `_space` keyword index with `is_tenant: true` (Qdrant's built-in multitenancy optimisation) | one query with `_space ∈ scope` |
| `per_space` | `lf_global_reference`, `lf_sch7_reference`, … | one query per readable space, results merged by the same RRF fusion already used for multi-query |

`knowledge_spaces.placement` lets *one* space go dedicated while the rest stay
shared, so a big school can be moved without moving everyone. Moving a space is a
`lessonforge-corpus move-space <id> --to dedicated` job: scroll the points with
their vectors, write them to the new collection, verify counts, delete the
originals. No re-embedding, no downtime. `corpus_sources` is the checklist.

Because the scope, IDs, ingestion, and registry are space-aware from day one,
neither change touches the chat, lesson, quiz, or unit code.

### What changes in today's code

| Where | Change |
|---|---|
| `providers/base.py` `VectorStore` | read methods take required `scope: AccessScope`; `delete` / `count` too |
| `providers/vectorstore/qdrant.py` | `_build_filter` ANDs `MatchAny(_space)`; payload index on `_space`; placement strategy behind `_name()` |
| `rag/retriever.py`, `rag/grounding.py`, `services/chat/retrieve.py` | pass `scope` through; fallback keeps it |
| `rag/documents.py` `content_id`, doc_id | include the space |
| `rag/ingest.py`, `/corpus/*` | `space` parameter, write policy, `corpus_sources` row, `_space` stamping |
| migration | `0003_knowledge_spaces.sql`, plus a one-off Qdrant job that sets `_space = "global"` on every existing point and creates the index |

### Edge cases decided now

- **Teacher at two schools:** if Edboox sends `school_ids: [...]`, the scope is
  `global` plus each school. `AccessScope` is already a list.
- **Student changes school:** the next token carries the new `school_id`, so the
  scope switches immediately. Old conversation citations stay in the student's own
  history only.
- **Caches:** any future retrieval or answer cache must key on `scope.spaces`.
  The planner cache stores plans, not content, so it is unaffected.
- **Activity events:** `corpus.ingested` gains `space`, and `chat.message` gains
  `spaces_hit` (for example, "answered from the school's own notes"), so Edboox
  can see whether private material is being used.

## 17. Sharing lesson plans within a school

### The rule

| Who | A teacher's **private** plan | A plan shared with the **school** |
|---|---|---|
| The owner | full access | full access |
| Another teacher, same school | not visible (404) | **read and copy** |
| A teacher at another school | not visible (404) | not visible (404) |
| A student | no access (no `author` permission) | no access |
| Superadmin | support access through the admin CLI only, audited | same |

Applies to both `lesson` and `unit` documents. Default is `private`.

### Why view-and-copy, not shared editing

- Teachers adapt a colleague's plan to their own class. A **copy** gives them that
  without changing the original under its author.
- Every document keeps one owner and one clean version history (undo/redo stay
  simple).
- `copied_from` records where a copy came from, so the UI can show "Based on
  Sita Sharma's plan" and Edboox can see which plans get reused.

### API

| Endpoint | Who | Effect |
|---|---|---|
| `PATCH /api/v1/documents/{id}` `{"visibility": "school" \| "private"}` | owner | share or un-share; sets/clears `shared_at`; emits `document.shared` / `document.unshared` |
| `GET /api/v1/documents?scope=mine` (default) | teacher | my documents, as today |
| `GET /api/v1/documents?scope=school&grade=&subject=&q=` | teacher | plans shared in **my** school, newest first, paginated; owner's display name included |
| `GET /api/v1/documents/{id}` | owner, or same-school teacher if shared | read (head version) |
| `POST /api/v1/documents/{id}/copy` | owner, or same-school teacher if shared | new `private` document owned by the caller, with the head snapshot as its first version and `copied_from` set; emits `document.copied` |

Edit, refine, undo, redo, delete, export of versions, and visibility changes stay
**owner-only**. A colleague who can read the plan gets **403** (they already know it
exists); anyone who can't read it gets **404**, consistent with "don't reveal what
you can't see". Exporting a shared plan to DOCX/PPTX is a read and is allowed.

### Enforcement: one access function

```python
class DocumentAccess:                     # services/documents/access.py
    def can_read(self, p: Principal, d: StoredDocument) -> bool:
        return d.owner_id == p.id or (
            d.visibility == "school" and d.tenant_id is not None
            and d.tenant_id == p.tenant_id)          # from the SIGNED token
    def can_write(self, p: Principal, d: StoredDocument) -> bool:
        return d.owner_id == p.id
```

`DocumentService` calls these for every operation. The store's list query mirrors
`can_read` in SQL (`owner_id = %s OR (visibility = 'school' AND tenant_id = %s)`),
and a test asserts both agree. `tenant_id` on a document is stamped from the
creator's signed `school_id` at creation and never changes.

### Edge cases

- **Teacher changes school:** shared plans stay in the old school's `tenant_id`.
  The teacher still owns and can edit them; new colleagues don't see them
  unless the teacher copies them (copies take the teacher's current school).
- **Teacher leaves (principal deleted):** their documents cascade-delete,
  including shared ones. Colleagues who **copied** keep their copies, because
  `copied_from` is set to NULL rather than deleting the copy.
- **A plan grounded on School A's private documents** (§16) is shared only inside
  School A, so the sharing rule never moves private material across schools.
- **Teacher at two schools** (`school_ids`): a plan is shared with the school it
  was created under. Choosing the school at share time is a later UI detail;
  the column already supports it.

### Later, without redesign

| Need | Change |
|---|---|
| Share with specific teachers or a department | a `document_shares (document_id, principal_id \| group_id, can)` table; `can_read` gains one `OR EXISTS` clause |
| Co-editing | `can: 'edit'` on that table; edits still go through the version graph, attributed per editor |
| School-approved "model plans" | a `school_admin` marks a plan `approved`, or promotes it into the school's knowledge space (§16) as an `exemplar` so the AI learns from it |
| Comments or likes | a separate table keyed by `document_id`; nothing above changes |

## 18. Implementation notes (how the design was built, and where it was refined)

**Where things live.** `auth/` (principal, policy, verifier, directory, replay guard,
service), `knowledge/` (scope, registry), `context/` (model, provider, service,
scope policy), `integrations/edboox.py`, `activity/` (events, store), `audit.py`,
`db/` (pool, migrations, `lessonforge-db`), `admin/cli.py` (`lessonforge-admin`),
`platform.py` (composition root for all of the above), `api/routers/*`,
`api/static/lf.js` (the one browser client), `dev/fake_edboox/`.

**Refinements to the design, with reasons:**

| Area | Design said | Built | Why |
|---|---|---|---|
| Scope plumbing (§16) | thread `RequestContext` through the services | one task-local `ContextVar` (`request_context.py`), bound once by the auth dependency, read only by the `Retriever`, the metered LLM and the activity recorder | the retrieval graph is composed once and shared by ~a dozen services; threading a scope argument through all of them would couple every service to authorization. A ContextVar is per-request-task (Starlette copies it into the threadpool and streaming generators; executor work uses `submit_in_context`), so nothing leaks between requests. It stays **fail-safe**: unbound reads get `AccessScope.public()` (shared shelf only). The `VectorStore` port still takes a **required** `scope` on every read and delete, and the adapter ANDs it in independently of `where`. Tested in `test_corpus_isolation.py` and `test_platform_ops.py`. |
| IDs of existing global chunks | hash the space into every id | hash the space into every id **except `global`** | re-ingesting the existing shared corpus after the upgrade stays idempotent (same ids), while schools still never collide. |
| `ord` allocation | `max(ord)+1` | counters `chat_conversations.message_count` / `documents.version_count`, allocated by one `UPDATE … RETURNING` | `max()+1` races under READ COMMITTED (two appends read the same max); the counter serialises on the row lock and makes "transcript length" free. Concurrency is tested against real Postgres. |
| Version history | list of full versions | `GET /documents/{id}/versions` returns `{items}` **without snapshots** | snapshots are whole lessons/units; one version is fetched with `?version=`. |
| Chat turn reads | whole transcript per turn | the transcript *tail* (window) per turn; the summariser reads only the range that scrolled out | a long conversation no longer costs O(n) per message. |
| Artifact summaries | — | attempts for all of a conversation's artifacts in **one** query | removed an N+1 on conversation reload. |
| Placement `per_space` (§16) | config switch | not built yet (only `shared`) | it is explicitly "later"; the `_name()`/scope seam in the Qdrant adapter is where it goes. |
| Context refresh | background | in-process executor with per-principal de-duplication | no stampede on a cold cache; requests never wait on Edboox. |
| Session `session.started` for bearer | first call of the day | de-duplicated per process | good enough for analytics; documented as approximate. |
| Superadmin before first login | — | `grant` creates a placeholder principal (`external_role = 'pending'`) that the first launch completes | lets the owner be granted during setup. |
| Fake Edboox DB | init script | idempotent `edboox-db-init` one-shot (compose `dev` profile) | Postgres init scripts only run on an empty volume. |

**Operations.**

```bash
lessonforge-db migrate | status            # versioned schema; the API refuses a stale one
lessonforge-admin grant --subject teacher:1 # superadmin (audited); also revoke/grants
lessonforge-admin disable|enable|forget --subject student:42
lessonforge-admin sync-context --subject student:42
lessonforge-admin purge                     # expired launch jtis + events past retention (cron)
lessonforge-admin backfill-spaces           # once, after upgrading an existing corpus
python -m lessonforge.api.contract [--write]  # check / publish docs/openapi.v1.json
```

### Owner console and the live sign-in demo (added after the first release)

**Owner console (`/admin`, permission `admin` — superadmin only).** Everything an
operator used to need a terminal for, built on ONE application service
(`services/admin.py: AdminService`) that the CLI now also uses, so both doors are
guarded and audited identically:

| Section | What it does |
|---|---|
| Overview | health (LLM, Qdrant, Postgres), schema status, Edboox signing keys held, people/content/corpus counts, settings in effect |
| People | everyone who has used LessonForge (search by name/id, role, school, status); per person: Edboox details, **Refresh from Edboox**, **Disable/Enable**, **Delete their data** (type the id to confirm) |
| Owners | superadmins, **read-only** — grant/revoke stay CLI-only (D5) |
| Knowledge | the record book per shelf/collection; remove a whole document |
| Usage | the same content-free reports Edboox pulls |
| Audit log | paged, filterable |
| Maintenance | clean-up (expired passes, events past retention), backfill shelves, migration status |

Guards: no one can disable/delete their own account; a superadmin can't be
disabled/deleted from the console (revoke with the CLI first). Adding/removing
**students and teachers is Edboox's job** — the fake Edboox has `/admin` for that
(its owner account is an Edboox admin).

**Live sign-in demo (OFF by default).** `LF_DEMO=true docker compose --profile dev
up -d` enables `/api/v1/demo/*` in LessonForge (404 otherwise; excluded from the API
contract) and `/demo` in the fake Edboox: a presenter-driven walkthrough where each
*Next* performs the real call — Edboox login, pass minting, JWKS, the **production**
`/auth/launch` code (traced, not re-implemented), two live attacks (replay, edited
pass) being refused, the context pull, and `/me`.

