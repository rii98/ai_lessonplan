# Chat streaming contract (`/api/v1/chat/conversations/{id}/message`)

A chat turn streams **Server-Sent Events** over a `POST` response
(`text/event-stream`). Read it with `fetch` + a stream reader, not `EventSource`
(which cannot send `Authorization`). Each frame is:

```
event: <type>
data: <json>

```

| `event` | `data` | When |
|---|---|---|
| `token` | string — a text delta of the reply | repeatedly while the answer streams |
| `sources` | `Citation[]` — `{n, source, collection, text, score, metadata}` | once, after a grounded answer |
| `intent` | `{skill, confidence, slots, source, reason}` | a skill (e.g. `quiz`) handles the turn |
| `status` | `{message}` | progress while a slow skill works |
| `artifact` | artifact summary `{id, kind, title, question_count, attempts, best, …}` | a skill produced an interactive artifact (persisted) |
| `awaiting` | `{skill, slot, slots}` | the skill needs one more answer; the next message is routed back |
| `done` | `{citations, mode, title, …}` | always last on success |
| `error` | `{code, message, request_id}` | the turn failed; never contains internal details |

Rules for clients:

- **New event types are additive** — ignore types you don't know.
- `error` may be followed by nothing; treat the stream as finished.
- Authentication, permission and validation failures happen *before* the stream
  starts and use the normal JSON error envelope (`{"error": {...}}`), not SSE.
- Retrieval scope may be constrained server-side (`scope_mode: "locked"` in
  `GET /api/v1/chat/config`); client `mode`/`filters` are then ignored for the
  bound keys.
