-- 0003 — learn mode sessions + the long-term memory layer (docs/LEARN_MODE.md §6, §7).
--
-- Every row is owned by a principal with ON DELETE CASCADE, so `lessonforge-admin
-- forget` (which deletes the principal) erases a learner's sessions, memories and
-- mastery with no extra code path.

-- ── learn mode ───────────────────────────────────────────────────────────────
CREATE TABLE learning_sessions (
  id              uuid PRIMARY KEY,
  conversation_id uuid NOT NULL REFERENCES chat_conversations(id) ON DELETE CASCADE,
  owner_id        uuid NOT NULL REFERENCES principals(id) ON DELETE CASCADE,
  status          text NOT NULL,
  topic           text NOT NULL,
  data            jsonb NOT NULL,                  -- the LearningSession document
  version         int  NOT NULL DEFAULT 0,         -- optimistic concurrency
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now()
);
-- at most ONE active session per conversation (paused ones may coexist)
CREATE UNIQUE INDEX uq_learning_sessions_active ON learning_sessions (conversation_id)
  WHERE status IN ('planning', 'active');
CREATE INDEX ix_learning_sessions_conv ON learning_sessions (conversation_id, updated_at DESC);
CREATE INDEX ix_learning_sessions_owner ON learning_sessions (owner_id, updated_at DESC);

-- append-only per-turn log: move, trace, grading, retrieval trigger (eval + analytics)
CREATE TABLE learning_events (
  id          bigserial PRIMARY KEY,
  session_id  uuid NOT NULL REFERENCES learning_sessions(id) ON DELETE CASCADE,
  data        jsonb NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_learning_events_session ON learning_events (session_id, id);

-- ── long-term memory ─────────────────────────────────────────────────────────
CREATE TABLE user_memories (
  id            uuid PRIMARY KEY,
  owner_id      uuid NOT NULL REFERENCES principals(id) ON DELETE CASCADE,
  kind          text NOT NULL,                     -- preference | learning_need | interest | goal | episode
  content       text NOT NULL,
  embedding     jsonb,                             -- float array; per-user brute-force recall
  source        jsonb NOT NULL DEFAULT '{}',       -- provenance: conversation / messages / session
  confidence    real NOT NULL DEFAULT 0.7,
  valid_from    timestamptz NOT NULL DEFAULT now(),
  superseded_by uuid,                              -- temporal validity: replaced, not erased
  last_used_at  timestamptz,
  expires_at    timestamptz,
  created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_user_memories_live ON user_memories (owner_id, created_at DESC)
  WHERE superseded_by IS NULL;

CREATE TABLE mastery_records (
  owner_id   uuid NOT NULL REFERENCES principals(id) ON DELETE CASCADE,
  kc_ref     text NOT NULL,                        -- curriculum standard code, or "free:<title>"
  title      text NOT NULL DEFAULT '',
  p_mastery  real NOT NULL,
  attempts   int  NOT NULL DEFAULT 0,
  correct    int  NOT NULL DEFAULT 0,
  stability  real NOT NULL DEFAULT 1,              -- days; grows with each successful review
  last_seen  timestamptz NOT NULL DEFAULT now(),
  due_at     timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (owner_id, kc_ref)
);
CREATE INDEX ix_mastery_due ON mastery_records (owner_id, due_at);

CREATE TABLE memory_settings (
  owner_id   uuid PRIMARY KEY REFERENCES principals(id) ON DELETE CASCADE,
  enabled    boolean NOT NULL DEFAULT true,
  updated_at timestamptz NOT NULL DEFAULT now()
);
