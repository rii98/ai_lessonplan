# Activity events (Contract 4, `docs/EDBOOX_INTEGRATION.md` §15)

Content-free usage events Edboox **pulls**:

- `GET /api/v1/events?after=<cursor>&limit=&types=` — raw, incremental, gap-free;
  store `next_cursor` and resume from it. Requires `reports.read`.
- `GET /api/v1/reports/usage|chat-topics|quizzes|users/{sub}/summary?from=&to=` —
  ready-made aggregates (range ≤ 366 days, default last 30).

Every event has: `id` (the cursor), `occurred_at`, `type`, `schema_ver`, `subject`
(Edboox `sub`), `role`, `tenant_id` (school), `grade`, `resource_type`,
`resource_id`, `props`.

**Privacy guarantee.** Each type's `props` is a Pydantic model that forbids
unknown keys and bounds every string, so message text, names and other free input
cannot be recorded — enforced by `tests/test_activity.py`, which walks the
whole catalogue. Deleting a principal anonymises their events (`subject` and
`principal_id` set to NULL by a database trigger) so school-level counts stay
right while nothing points back to the child. Raw events are deleted after
`activity.retention_days` (`lessonforge-admin purge`).

**Visibility.** A caller with a signed school sees only that school; the Edboox
`service` principal and superadmin see all schools (optionally `school=`).

| type | schema_ver | props |
|---|---|---|
| `session.started` | 1 | `channel` |
| `chat.conversation.created` | 1 | `scope_mode`, `grade`, `subject` |
| `chat.message` | 1 | `intent`, `intent_source`, `confidence`, `subject`, `chapters`, `collections`, `spaces_hit`, `citations`, `grounded`, `latency_ms`, `tokens_in`, `tokens_out`, `model` |
| `quiz.created` | 1 | `source`, `questions`, `question_types`, `difficulty`, `subject`, `chapter` |
| `quiz.attempt.started` | 1 | `quiz_id` |
| `quiz.attempt.finished` | 1 | `score`, `total`, `percent`, `duration_s`, `retry` |
| `lesson.generated` | 1 | `grade`, `subject`, `days`, `latency_ms`, `tokens_in`, `tokens_out` |
| `unit.generated` | 1 | `grade`, `subject`, `days`, `latency_ms`, `tokens_in`, `tokens_out` |
| `document.refined` | 1 | `section`, `kind` |
| `document.shared` | 1 | `kind`, `grade`, `subject` |
| `document.unshared` | 1 | `kind`, `grade`, `subject` |
| `document.copied` | 1 | `kind`, `grade`, `subject`, `source_owner` |
| `artifact.exported` | 1 | `artifact`, `format` |
| `corpus.ingested` | 1 | `collection`, `space`, `documents`, `chunks` |

Changing a type incompatibly bumps its `schema_ver`; adding an optional prop does not.
