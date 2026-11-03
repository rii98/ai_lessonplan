# Try it yourself — a hands-on tour

A guided playground for everything the Edboox integration added. Each section
says **what to click**, **what you should see**, and **why it matters**. No code
knowledge needed.

> Two apps run side by side:
> - **Edboox** (the school app, a fake one for testing): http://localhost:8200
> - **LessonForge** (the AI helper): http://localhost:8000
>
> You always enter LessonForge **through Edboox** — LessonForge has no passwords.

---

## 0. Start everything

```bash
# with the live sign-in demo switched ON (for section 9)
LF_PG_HOST_PORT=55432 LF_DEMO=true docker compose --profile dev up -d

# or normal mode (demo off — the default)
LF_PG_HOST_PORT=55432 docker compose --profile dev up -d
```

The AI answers come from Ollama on your computer — keep `ollama serve` (or the
desktop app) running for chat and lesson generation.

**Ready check:** http://localhost:8000/health/ready shows `{"status":"ok","database":true}`.

### Who you can sign in as

Every password is **`password`**.

| Username | Who | School | Notes |
|---|---|---|---|
| `owner` | Platform Owner (you) | sch-7 | superadmin — sees everything |
| `sita` | Sita Sharma, teacher | sch-7 | teaches Class 8 & 10 Science |
| `ram` | Ram Thapa, teacher | sch-7 | teaches Class 8 Maths |
| `maya` | Maya Gurung, teacher | sch-9 | **a different school** |
| `aarav`, `bikash` | students | sch-7 | Class 8 |
| `gita` | student | sch-7 | Class 10 |
| `nima` | student | sch-9 | Class 10 |

**How to switch person:** open http://localhost:8200/logout, then sign in as someone else.
(Tip: use a private/incognito window per person to stay signed in as two people at once.)

---

## 1. Sign in through Edboox

1. Open http://localhost:8200 and sign in as **aarav**.
2. Click **🤖 Open AI assistant**.

**You should see:** LessonForge opens on **Chat**. The menu shows only *Chat*, and
the top bar says *Aarav Karki · student*.

**Try:** type http://localhost:8000/ (the lesson editor) in the address bar → you're
sent straight back to Chat. Students can only chat.

**Why it matters:** Edboox vouched for Aarav with a sealed one-minute pass;
LessonForge checked it and decided what a student may do.

---

## 2. Chat as a student (personalised from Edboox)

Still as **aarav**, start a **New chat** and ask: *"Do plants get their food from the soil?"*

**You should see:** a streamed answer with numbered sources underneath.

**Notice:** the new chat's scope is already set to **grade 8** — LessonForge asked
Edboox about Aarav in the background and learned his class. You can still change it.

---

## 3. Teachers: a lesson that already knows you

1. Sign in as **sita** → **Open AI assistant**. You land on the **Lesson editor**.
2. Open **Profile** — default grade **8**, subject **Science**, language **ne**,
   school *Shree Janata…* are filled in. These came from Sita's teaching
   assignments in Edboox.
3. Type a topic only (e.g. *Photosynthesis*) and generate.

**You should see:** a full lesson for Grade 8 Science — you never typed the grade.

Then open **Library** → your saved lessons with their full edit history
(undo / redo / export).

---

## 4. Share a lesson with your school

1. As **sita**, in **Library**, open a lesson → click **🏫 Share with my school**.
   It now shows *● shared*.
2. Sign in as **ram** (same school) → **Library** → tab **Shared in my school**.
   - You see Sita's lesson *by Sita Sharma*.
   - You can **read** and **export** it, and **📄 Copy to my work**.
   - There are **no** edit, undo or delete buttons — it's hers.
3. Copy it, then check **My work** — the copy is yours and private.
4. Sign in as **maya** (other school) → **Shared in my school** is **empty**.
5. Back as **sita**, click **🔒 Make private** → Ram's shared tab is empty again.

**Why it matters:** teachers reuse good work without anyone editing someone else's plan,
and plans never cross school walls.

---

## 5. Books and "shelves" (owner)

1. Sign in as **owner** → **Open AI assistant** → **Knowledge base** in the menu
   (http://localhost:8000/corpus).
2. At the top, **Knowledge space**:
   - *Shared curriculum (global)* — every school sees it.
   - *＋ A school's private shelf…* → type `sch-7` → only School sch-7 sees it.
3. Add one record on the **global** shelf, for example:
   *"Common misconception: plants eat soil. Correct: they make food by photosynthesis."*
   with subject `Science`, grade `8`.
4. Switch the shelf to **school:sch-7** and add a different record, e.g.
   *"Our school garden at Pokhara shows photosynthesis every morning."*

**Test the walls:**
- As **sita** (sch-7), generate on *photosynthesis* and open its sources → both records can appear.
- As **maya** (sch-9), do the same → **only** the global one, never the sch-7 note.

**Try breaking it:** add a record with subject `Astrology` → refused with
*unknown subject…* (only real subjects are allowed, so filters always match).

Load the starter books in one go (optional):
```bash
docker compose exec api lessonforge-ingest --seed
```

---

## 6. The Owner console (no terminal needed)

As **owner**, open **http://localhost:8000/admin** (the Library and Units pages also show an **Owner** tab).

| Section | Things to try |
|---|---|
| 📊 **Overview** | Green dots for AI model, search database, Postgres; Edboox key `edboox-dev-1`; counts of people, lessons, chats. |
| 👥 **People** | Search `aarav`. Open him → see what Edboox told us (grade, section, school). Click **↻ Refresh from Edboox**. |
| | Click **⏸ Disable** → as Aarav, LessonForge now refuses him (within a minute). Click **Enable** to undo. |
| | Try disabling **yourself** → refused: *you cannot disable your own account*. |
| 🔑 **Owners** | Lists superadmins. Read-only by design — changing owners needs the server command shown on the page. |
| 📚 **Knowledge** | Every document on every shelf. **Remove** one → its pieces disappear from search. |
| 📈 **Usage** | Chats, quizzes, lessons and AI tokens by day / school / role; the chapters students ask about. |
| 🧾 **Audit log** | Every serious action you just did — who, what, when. |
| 🧹 **Maintenance** | **Run clean-up**, **Backfill shelves**, database status. |

---

## 7. Add and remove people (Edboox admin)

Edboox owns students and teachers, so this lives in Edboox.

1. Sign in to Edboox as **owner** → click **🏫 Manage students & teachers**
   (http://localhost:8200/admin).
2. **Add a student**: name *Sunita Tamang*, username `sunita`, school `sch-7`,
   class *Class 9* → **Add student**. You see *Added student:5* (the number may differ).
3. Sign in as **sunita** / `password` → **Open AI assistant** → she's in LessonForge,
   her chats start at **grade 9**. No setup was needed in LessonForge.
4. **Add a teacher** the same way; in the *Teaches* box write one class per line:
   `Class 9 | A | Science and Technology`.

**Removing someone completely takes two steps** (the apps never share data):
1. Edboox admin → **Remove** (removes them from Edboox).
2. LessonForge Owner console → **People** → open them → **🗑 Delete their data** →
   type their id (e.g. `student:5`) to confirm.
   Their lessons and chats are deleted; usage totals stay, without their name.

---

## 8. See what Edboox sees (usage, no private words)

1. Sign in to Edboox as any **teacher** → click **📈 AI usage report**
   (http://localhost:8200/usage).
2. You see activity by day / school / role and a list of recent events.

**Check the privacy rule:** chat as a student with a sentence like
*"MY SECRET QUESTION about leaves"*, then reload the usage page → the event shows
*chat.message* with counts and tokens, **never your words**.

---

## 9. The live sign-in demo (show it to someone!)

Requires starting with `LF_DEMO=true` (section 0). Open **http://localhost:8200/demo**.

1. Choose a person, press **Start ▶**, then **Next ▶** step by step:

| Step | What happens for real |
|---|---|
| 1 | Sign in to Edboox (password checked inside Edboox only) |
| 2 | Edboox makes a **60-second pass** — shown in colour, with a countdown |
| 3 | Edboox's **public key** — can check signatures, can never create them |
| 4 | The pass goes to LessonForge |
| 4.1–4.7 | LessonForge's **real** checks, one per click: read → issuer → keys → signature & expiry & one-time use → who → shelves → session |
| 5 | 🕵️ **Attack:** reuse the same pass → **refused** (*already used*) |
| 6 | 🕵️ **Attack:** edit the pass to say *superadmin* → **refused** (*signature verification failed*) |
| 7 | LessonForge asks Edboox about the person — see `"Class 10"` become `10` |
| 8 | Signed in → **Open LessonForge as …** |

2. The middle black panel is the **wire**: every message between the two apps, live.
3. Press **↺ Restart** to do it again with someone else.

If you wait more than 60 seconds between steps 2 and 4, LessonForge refuses the
expired pass — that's a good thing to show too. Press **↺ Restart** for a new one.

---

## 10. For the curious: poke at the security from a terminal

```bash
# Not signed in → refused, with a pointer to Edboox's login
curl -s http://localhost:8000/api/v1/me

# A fake token → refused
curl -s -H "Authorization: Bearer not-a-real-token" http://localhost:8000/api/v1/me
```

Interactive API documentation: **http://localhost:8000/docs** (every endpoint under `/api/v1`).

---

## Reset or stop

```bash
# stop (data is kept)
LF_PG_HOST_PORT=55432 docker compose --profile dev down

# start again with the demo OFF (the default)
LF_PG_HOST_PORT=55432 docker compose --profile dev up -d

# wipe EVERYTHING (all lessons, chats, books, Edboox users) and start fresh
LF_PG_HOST_PORT=55432 docker compose --profile dev down -v
```

## Where to read more

- Plain-language story: [`EDBOOX_OVERVIEW.md`](EDBOOX_OVERVIEW.md)
- Full design and decisions: [`EDBOOX_INTEGRATION.md`](EDBOOX_INTEGRATION.md)
- What Edboox's usage feed contains: [`ACTIVITY_EVENTS.md`](ACTIVITY_EVENTS.md)
