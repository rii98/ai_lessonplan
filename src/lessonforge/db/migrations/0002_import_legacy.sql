-- 0002 — import rows written by the pre-migration stores (untyped text columns,
-- owner_id = 'default' or any free-form string), then drop the legacy tables.
--
-- Every legacy owner becomes a principal (issuer 'local', subject = the old
-- owner string) so dev data survives and stays attributable. A database that never
-- had legacy tables (every fresh install) makes this a no-op.

DO $$
BEGIN
  IF to_regclass('legacy_chat_conversations') IS NULL
     AND to_regclass('legacy_documents') IS NULL THEN
    RETURN;
  END IF;

  -- one 'local' principal per legacy owner string
  CREATE TEMP TABLE _legacy_owners (owner text PRIMARY KEY) ON COMMIT DROP;
  IF to_regclass('legacy_chat_conversations') IS NOT NULL THEN
    INSERT INTO _legacy_owners SELECT DISTINCT owner_id FROM legacy_chat_conversations
      ON CONFLICT DO NOTHING;
  END IF;
  IF to_regclass('legacy_documents') IS NOT NULL THEN
    INSERT INTO _legacy_owners SELECT DISTINCT owner_id FROM legacy_documents
      ON CONFLICT DO NOTHING;
  END IF;
  INSERT INTO principals (issuer, subject, external_role, display_name)
  SELECT 'local', owner, 'teacher', owner FROM _legacy_owners
  ON CONFLICT (issuer, subject) DO NOTHING;

  IF to_regclass('legacy_documents') IS NOT NULL THEN
    INSERT INTO documents (id, owner_id, kind, title, grade, subject, head_version_id,
                           created_at, updated_at)
    SELECT d.id::uuid, p.id, d.kind, d.title, d.grade, d.subject, d.head_version_id::uuid,
           d.created_at::timestamptz, d.updated_at::timestamptz
      FROM legacy_documents d
      JOIN principals p ON p.issuer = 'local' AND p.subject = d.owner_id;

    INSERT INTO document_versions (id, document_id, ord, parent_id, origin, snapshot,
                                   instruction, diff, grounding_sources, scores, created_at)
    SELECT v.id::uuid, v.document_id::uuid, v.ord, v.parent_id::uuid, v.origin,
           v.snapshot::jsonb, v.instruction, v.diff::jsonb, v.grounding_sources::jsonb,
           v.scores::jsonb, v.created_at::timestamptz
      FROM legacy_document_versions v
      JOIN legacy_documents d ON d.id = v.document_id;

    UPDATE documents d SET version_count = (
      SELECT COALESCE(max(v.ord) + 1, 0) FROM document_versions v WHERE v.document_id = d.id);

    DROP TABLE legacy_document_versions;
    DROP TABLE legacy_documents;
  END IF;

  IF to_regclass('legacy_chat_conversations') IS NOT NULL THEN
    INSERT INTO chat_conversations (id, owner_id, title, defaults, summary, created_at, updated_at)
    SELECT c.id::uuid, p.id, c.title, c.defaults::jsonb, c.summary,
           c.created_at::timestamptz, c.updated_at::timestamptz
      FROM legacy_chat_conversations c
      JOIN principals p ON p.issuer = 'local' AND p.subject = c.owner_id;

    INSERT INTO chat_messages (id, conversation_id, ord, role, content, citations, meta, created_at)
    SELECT m.id::uuid, m.conversation_id::uuid, m.ord, m.role, m.content,
           m.citations::jsonb, m.meta::jsonb, m.created_at::timestamptz
      FROM legacy_chat_messages m
      JOIN legacy_chat_conversations c ON c.id = m.conversation_id;

    UPDATE chat_conversations c SET message_count = (
      SELECT COALESCE(max(m.ord) + 1, 0) FROM chat_messages m WHERE m.conversation_id = c.id);

    IF to_regclass('legacy_chat_artifacts') IS NOT NULL THEN
      INSERT INTO chat_artifacts (id, conversation_id, message_id, kind, title, payload, meta,
                                  created_at)
      SELECT a.id::uuid, a.conversation_id::uuid,
             (SELECT m.id FROM chat_messages m WHERE m.id = a.message_id::uuid),
             a.kind, a.title, a.payload::jsonb, a.meta::jsonb, a.created_at::timestamptz
        FROM legacy_chat_artifacts a
        JOIN legacy_chat_conversations c ON c.id = a.conversation_id;

      INSERT INTO chat_attempts (id, artifact_id, conversation_id, owner_id, kind, status,
                                 score, total, state, created_at, updated_at)
      SELECT t.id::uuid, t.artifact_id::uuid, t.conversation_id::uuid, conv.owner_id, t.kind,
             t.status, t.score, t.total, t.state::jsonb,
             t.created_at::timestamptz, t.updated_at::timestamptz
        FROM legacy_chat_attempts t
        JOIN chat_conversations conv ON conv.id = t.conversation_id::uuid
        JOIN chat_artifacts a ON a.id = t.artifact_id::uuid;
      DROP TABLE legacy_chat_attempts;
      DROP TABLE legacy_chat_artifacts;
    END IF;

    DROP TABLE legacy_chat_messages;
    DROP TABLE legacy_chat_conversations;
  END IF;
END $$;
