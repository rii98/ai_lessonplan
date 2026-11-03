-- Long-term memory v2 (docs/MEMORY.md): reconciled writes, importance, a synthesised
-- profile digest, generation counters for cross-process recall caching, soft
-- retirement + retention, and embedding-model provenance.

-- items: importance (≠ confidence), which embedder made the vector, and an explicit
-- "no longer live" marker shared by supersede / retire / evict.
ALTER TABLE user_memories
  ADD COLUMN importance      real NOT NULL DEFAULT 0.5,
  ADD COLUMN embedding_model text,
  ADD COLUMN ended_at        timestamptz,
  ADD COLUMN end_reason      text;              -- superseded | retired | evicted

UPDATE user_memories SET ended_at = created_at, end_reason = 'superseded'
 WHERE superseded_by IS NOT NULL AND ended_at IS NULL;

DROP INDEX IF EXISTS ix_user_memories_live;
CREATE INDEX ix_user_memories_live ON user_memories (owner_id, created_at DESC)
  WHERE ended_at IS NULL;
-- retention sweeps: dead history and expired episodes
CREATE INDEX ix_user_memories_ended ON user_memories (ended_at) WHERE ended_at IS NOT NULL;
CREATE INDEX ix_user_memories_expires ON user_memories (expires_at) WHERE expires_at IS NOT NULL;

-- per-learner generation counters: ``version`` bumps on every item change (keys the
-- recall cache in every replica); ``removal_version`` on every change that removes or
-- rewrites an item (a digest older than it is not shown).
ALTER TABLE memory_settings
  ADD COLUMN version         bigint NOT NULL DEFAULT 0,
  ADD COLUMN removal_version bigint NOT NULL DEFAULT 0;

-- the always-on profile digest, rebuilt off the request path
CREATE TABLE memory_profiles (
  owner_id      uuid PRIMARY KEY REFERENCES principals(id) ON DELETE CASCADE,
  content       text NOT NULL,
  built_version bigint NOT NULL,
  built_at      timestamptz NOT NULL DEFAULT now()
);
