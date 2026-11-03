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
| `learn_state` | the session's student-safe view `{id, status, goal, topic, ungrounded, pace, cursor, objectives[{id,title,status,early,mastery}], mastered, total, parking_lot, choices}` | every learn-mode turn (docs/LEARN_MODE.md) |
| `learn_widget` | an inline check: the quiz play-view shape + `{kind, label, purpose, objective_id, ask_confidence}` — **never the answer** | after the tutor's text, when the move shows a check |
| `learn_feedback` | `{widget_id, correct, score, expected, explanation, feedback, reveal}` | first event of a turn that answered a widget |
| `learn_offer` | `{topic}` | the router suggests a lesson; the UI shows a "Start lesson" button |

Rules for clients:

- **New event types are additive** — ignore types you don't know.
- `error` may be followed by nothing; treat the stream as finished.
- Authentication, permission and validation failures happen *before* the stream
  starts and use the normal JSON error envelope (`{"error": {...}}`), not SSE.
- Retrieval scope may be constrained server-side (`scope_mode: "locked"` in
  `GET /api/v1/chat/config`); client `mode`/`filters` are then ignored for the
  bound keys.


## Learn mode (docs/LEARN_MODE.md)

Three more routes stream the SAME event protocol:

| Route | Body | What |
|---|---|---|
| `POST /api/v1/chat/conversations/{id}/learning-sessions` | `{topic, mode?, filters?}` | plan a lesson and stream its opening turn (a running one is paused, not lost) |
| `POST /api/v1/chat/conversations/{id}/learn` | `{widget_response: {widget_id, response, confidence?}}` **or** `{action, action_value?}` | answer the widget on screen, or press a quick action: `hint · example · skip · faster · slower · pause · end · choose · resume` |
| `POST /api/v1/chat/conversations/{id}/message` | unchanged | while a lesson is **active**, typed replies go to the tutor (no router, no QA retrieval); a **paused** lesson leaves the chat free |

Plus `GET …/conversations/{id}/learning-session` → `{session, widget?}` (re-shows the widget
awaiting an answer after a reload) and `POST /api/v1/chat/learning-sessions/{id}/end`.
The learner's memory: `GET/DELETE /api/v1/me/memories[/{id}]`, `PUT /api/v1/me/memory-settings`,
`GET /api/v1/me/learning` (mastery, soonest review first).
