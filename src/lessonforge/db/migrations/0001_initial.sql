-- 0001 — the typed LessonForge schema (docs/EDBOOX_INTEGRATION.md §7, §15, §16).
--
-- Conventions: uuid primary keys, timestamptz, jsonb for nested domain models,
-- FKs with ON DELETE CASCADE where a child cannot outlive its parent (deleting a
-- principal is one statement), CHECK constraints for small enums, and an index on
-- every foreign key that is ever used to find or delete rows.

-- Stores before migrations existed created untyped tables with these names.
-- Move them aside; 0002 imports their rows into the typed tables, then drops them.
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['chat_conversations','chat_messages','chat_artifacts',
                           'chat_attempts','documents','document_versions'] LOOP
    IF to_regclass(t) IS NOT NULL AND to_regclass('legacy_' || t) IS NULL THEN
      EXECUTE format('ALTER TABLE %I RENAME TO %I', t, 'legacy_' || t);
    END IF;
  END LOOP;
END $$;
DROP INDEX IF EXISTS ix_chat_messages_conv, ix_chat_artifacts_conv,
                     ix_chat_attempts_artifact, ix_document_versions_doc;

-- ── identity, access, context ───────────────────────────────────────────────
CREATE TABLE principals (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  issuer            text NOT NULL,                 -- configured issuer name: 'edboox'
  subject           text NOT NULL,                 -- token 'sub': 'student:42'
  external_role     text NOT NULL,                 -- mapped role from the latest token
  display_name      text,
  tenant_id         text,                          -- the signed school id
  context           jsonb NOT NULL DEFAULT '{}',   -- UserContext (our vocabulary)
  context_synced_at timestamptz,
  disabled_at       timestamptz,                   -- local kill switch → 403
  created_at        timestamptz NOT NULL DEFAULT now(),
  last_seen_at      timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_principals_issuer_subject UNIQUE (issuer, subject)
);
CREATE INDEX ix_principals_tenant ON principals (tenant_id) WHERE tenant_id IS NOT NULL;

CREATE TABLE role_grants (                         -- LOCAL elevation only (D5)
  principal_id uuid NOT NULL REFERENCES principals(id) ON DELETE CASCADE,
  role         text NOT NULL CHECK (role IN ('superadmin')),
  granted_by   text NOT NULL,                      -- operator who ran the CLI
  granted_at   timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (principal_id, role)
);

CREATE TABLE used_token_ids (                      -- launch-token replay guard
  issuer     text NOT NULL,
  jti        text NOT NULL,
  expires_at timestamptz NOT NULL,                 -- purged after this
  PRIMARY KEY (issuer, jti)
);
CREATE INDEX ix_used_token_ids_expiry ON used_token_ids (expires_at);

CREATE TABLE audit_log (                           -- privileged actions only
  id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  principal_id uuid REFERENCES principals(id) ON DELETE SET NULL,
  actor        text NOT NULL,
  action       text NOT NULL,                      -- 'corpus.ingest', 'grant.add', …
  target       text,
  detail       jsonb NOT NULL DEFAULT '{}',
  at           timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_audit_log_at ON audit_log (at);
CREATE INDEX ix_audit_log_principal ON audit_log (principal_id) WHERE principal_id IS NOT NULL;

-- ── knowledge spaces (§16) ──────────────────────────────────────────────────
CREATE TABLE knowledge_spaces (
  id         text PRIMARY KEY,                     -- 'global' | 'school:sch-7'
  kind       text NOT NULL CHECK (kind IN ('global', 'school')),
  tenant_id  text,                                 -- NULL for global
  name       text NOT NULL,
  placement  text,                                 -- NULL = vector_store default
  status     text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'archived')),
  created_at timestamptz NOT NULL DEFAULT now(),
  CHECK ((kind = 'global') = (tenant_id IS NULL))
);
INSERT INTO knowledge_spaces (id, kind, tenant_id, name)
VALUES ('global', 'global', NULL, 'Shared curriculum');

CREATE TABLE corpus_sources (                      -- one row per ingested document
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  space_id     text NOT NULL REFERENCES knowledge_spaces(id),
  collection   text NOT NULL,
  doc_id       text NOT NULL,                      -- the doc_id stamped on its chunks
  title        text NOT NULL,
  source_label text,
  checksum     text NOT NULL,                      -- sha256 of the original content
  chunks       int  NOT NULL CHECK (chunks >= 0),
  ingested_by  uuid REFERENCES principals(id) ON DELETE SET NULL,
  ingested_at  timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_corpus_sources_doc UNIQUE (space_id, collection, doc_id)
);
CREATE INDEX ix_corpus_sources_ingested_by ON corpus_sources (ingested_by)
  WHERE ingested_by IS NOT NULL;

-- ── teacher data ────────────────────────────────────────────────────────────
CREATE TABLE teacher_profiles (
  principal_id uuid PRIMARY KEY REFERENCES principals(id) ON DELETE CASCADE,
  data         jsonb NOT NULL,                     -- TeacherProfile
  updated_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE documents (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id        uuid NOT NULL REFERENCES principals(id) ON DELETE CASCADE,
  tenant_id       text,                            -- creator's signed school id; never changes
  kind            text NOT NULL CHECK (kind IN ('lesson', 'unit')),
  title           text NOT NULL,
  grade           int,
  subject         text,
  head_version_id uuid,
  version_count   int  NOT NULL DEFAULT 0,         -- allocates document_versions.ord atomically
  visibility      text NOT NULL DEFAULT 'private' CHECK (visibility IN ('private', 'school')),
  shared_at       timestamptz,
  copied_from     uuid REFERENCES documents(id) ON DELETE SET NULL,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),
  CHECK (visibility = 'private' OR tenant_id IS NOT NULL),  -- can't share without a school
  CHECK ((visibility = 'school') = (shared_at IS NOT NULL))
);
-- keyset pagination: (updated_at, id) DESC within an owner
CREATE INDEX ix_documents_owner ON documents (owner_id, updated_at DESC, id DESC);
CREATE INDEX ix_documents_school_shared ON documents (tenant_id, shared_at DESC, id DESC)
  WHERE visibility = 'school';
CREATE INDEX ix_documents_copied_from ON documents (copied_from) WHERE copied_from IS NOT NULL;

CREATE TABLE document_versions (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  document_id       uuid NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  ord               int  NOT NULL,
  parent_id         uuid REFERENCES document_versions(id),
  origin            text NOT NULL CHECK (origin IN ('generate', 'refine', 'manual', 'copy')),
  snapshot          jsonb NOT NULL,
  instruction       text NOT NULL DEFAULT '',
  diff              jsonb NOT NULL DEFAULT '{}',
  grounding_sources jsonb NOT NULL DEFAULT '[]',
  scores            jsonb NOT NULL DEFAULT '{}',
  created_at        timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_document_versions_ord UNIQUE (document_id, ord)
);
CREATE INDEX ix_document_versions_parent ON document_versions (parent_id)
  WHERE parent_id IS NOT NULL;
ALTER TABLE documents
  ADD CONSTRAINT fk_documents_head FOREIGN KEY (head_version_id)
  REFERENCES document_versions(id) ON DELETE SET NULL DEFERRABLE INITIALLY DEFERRED;

-- ── chat ────────────────────────────────────────────────────────────────────
CREATE TABLE chat_conversations (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id   uuid NOT NULL REFERENCES principals(id) ON DELETE CASCADE,
  title      text NOT NULL,
  defaults   jsonb NOT NULL,                       -- ChatDefaults (pre-filled by ScopePolicy)
  summary    text NOT NULL DEFAULT '',
  message_count int NOT NULL DEFAULT 0,            -- allocates chat_messages.ord atomically
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_chat_conv_owner ON chat_conversations (owner_id, updated_at DESC, id DESC);

CREATE TABLE chat_messages (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  conversation_id uuid NOT NULL REFERENCES chat_conversations(id) ON DELETE CASCADE,
  ord             int  NOT NULL,
  role            text NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
  content         text NOT NULL,
  citations       jsonb NOT NULL DEFAULT '[]',
  meta            jsonb NOT NULL DEFAULT '{}',
  created_at      timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_chat_messages_ord UNIQUE (conversation_id, ord)
);

CREATE TABLE chat_artifacts (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  conversation_id uuid NOT NULL REFERENCES chat_conversations(id) ON DELETE CASCADE,
  message_id      uuid REFERENCES chat_messages(id) ON DELETE SET NULL,
  kind            text NOT NULL,
  title           text NOT NULL,
  payload         jsonb NOT NULL,
  meta            jsonb NOT NULL DEFAULT '{}',
  created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_chat_artifacts_conv ON chat_artifacts (conversation_id, created_at);
CREATE INDEX ix_chat_artifacts_message ON chat_artifacts (message_id) WHERE message_id IS NOT NULL;

CREATE TABLE chat_attempts (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
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
CREATE INDEX ix_chat_attempts_artifact ON chat_attempts (artifact_id, created_at DESC);
CREATE INDEX ix_chat_attempts_conv ON chat_attempts (conversation_id);
CREATE INDEX ix_chat_attempts_owner ON chat_attempts (owner_id);

-- ── activity (§15): content-free usage events Edboox pulls ───────────────────
CREATE TABLE activity_events (
  id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,  -- also the feed cursor
  occurred_at   timestamptz NOT NULL DEFAULT now(),
  type          text NOT NULL,
  schema_ver    smallint NOT NULL DEFAULT 1,
  principal_id  uuid REFERENCES principals(id) ON DELETE SET NULL,
  subject       text,                              -- Edboox 'sub', denormalised
  role          text NOT NULL,
  tenant_id     text,
  grade         int,
  resource_type text,
  resource_id   uuid,
  props         jsonb NOT NULL DEFAULT '{}',
  request_id    text
);
CREATE INDEX ix_activity_tenant_time ON activity_events (tenant_id, occurred_at);
CREATE INDEX ix_activity_type_time   ON activity_events (type, occurred_at);
CREATE INDEX ix_activity_subject     ON activity_events (subject, occurred_at);
CREATE INDEX ix_activity_tenant_id   ON activity_events (tenant_id, id);
CREATE INDEX ix_activity_principal   ON activity_events (principal_id)
  WHERE principal_id IS NOT NULL;

-- Deleting a principal anonymises their events (school-level counts stay right,
-- nothing points back to the child) — enforced here so no code path can forget.
CREATE FUNCTION anonymise_activity_of_principal() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  UPDATE activity_events SET principal_id = NULL, subject = NULL
   WHERE principal_id = OLD.id;
  RETURN OLD;
END $$;
CREATE TRIGGER trg_principals_anonymise_activity
  BEFORE DELETE ON principals
  FOR EACH ROW EXECUTE FUNCTION anonymise_activity_of_principal();
