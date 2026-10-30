# Fake Edboox (dev only)

A stand-in for the real Edboox so the whole integration runs locally. It shares
nothing with LessonForge except HTTP and the contracts in
[`docs/EDBOOX_INTEGRATION.md`](../../docs/EDBOOX_INTEGRATION.md), has its own
database (`edboox`) and its own RSA signing key (kept on the `edboox_data` volume),
and must never import `lessonforge`.

```bash
docker compose --profile dev up --build
# Edboox:      http://localhost:8200  (users below, password "password")
# LessonForge: http://localhost:8000  (reach it via "Open AI assistant")
```

| user | role | school | notes |
|---|---|---|---|
| owner | teacher | sch-7 | `teacher:1`, granted superadmin by the dev seed |
| sita | teacher | sch-7 | teaches Class 8 & 10 Science and Technology |
| ram | teacher | sch-7 | teaches Class 8 Compulsory Mathematics |
| maya | teacher | sch-9 | another school (isolation demos) |
| aarav, bikash | student | sch-7 | Class 8 |
| gita | student | sch-7 | Class 10 |
| nima | student | sch-9 | Class 10 |

`/usage` (teachers) pulls LessonForge's activity feed and usage report with a
`service` token — the same pull the real Edboox would do.
