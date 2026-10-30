# LessonForge + Edboox, from start to end

> A plain-language walkthrough of how the two apps work together. For the
> technical design, see [`EDBOOX_INTEGRATION.md`](EDBOOX_INTEGRATION.md).

## The two apps

- **Edboox** is the school app. It knows every school, teacher, and student: names, grades, classes, and what each teacher teaches.
- **LessonForge** is our AI helper. It answers questions, makes quizzes, writes lesson plans, and reads the textbooks to do it.

They are kept **separate**. Neither can look inside the other's storage. They only talk by sending messages to each other, like two offices exchanging letters.

## Step 0: Before anyone uses it, we stock the library

We, the owners, are the only people who can **add books and materials**. Every document goes on a **shelf**:

| Shelf | What's on it | Who can see it |
|---|---|---|
| **Shared shelf** | Government curriculum, approved textbooks, teaching guides | Every school |
| **School A's shelf** | School A's own notes and materials | Only School A |
| **School B's shelf** | School B's own notes and materials | Only School B |

Every time we add something, it's written in a **record book**: who added what, to which shelf, and when.

## Step 1: Someone logs in

1. Sita, a Grade 8 student at School A, logs into **Edboox** with her username and password.
2. She clicks **"AI Assistant"**.
3. Edboox gives her a **sealed pass** that says: *"This is Sita, a student, Grade 8, School A."*
   - Only Edboox can make this seal, and anyone can check that it's real.
   - The pass works **once**, and only for **one minute**, so a stolen pass is useless.
4. LessonForge checks the seal. If it's genuine, Sita is in. If it's fake, old, or reused, she's turned away.
5. LessonForge **never sees her password**. Edboox vouches for her.

## Step 2: LessonForge decides what she's allowed to do

Edboox only says **who** she is. LessonForge decides **what she can use**:

| Person | Can use |
|---|---|
| **Student** | Chat only, including quizzes inside chat |
| **Teacher** | Chat, lesson plans, multi-day units, quizzes, worksheets, downloads |
| **Owners (us)** | Everything, including adding books |

"Owner" is something **only we can give ourselves**, from our own computer. Edboox can never make anyone an owner, so even if Edboox were hacked, nobody could get in and change the books.

## Step 3: LessonForge gets to know her

The pass only carries the basics. In the background, LessonForge asks Edboox for a few more useful details:

- **Students:** grade, section, school area, whether they study in Nepali or English medium, stream (for Grades 11–12), and preferred language.
- **Teachers:** which subjects and grades they teach.

LessonForge **remembers** these and refreshes them once a day. If Edboox is busy or down, it uses what it remembered.

What we **never ask for**: birthdays, addresses, phone numbers, parents' details, photos, or marks.

These details are only used to make answers **more relevant**. They never decide what someone is allowed to do; only the sealed pass does that.

Edboox might write "Class 8" or "Science and Technology" while our books say "8" or "Science". A built-in **translation list** makes sure both sides mean the same thing.

## Step 4: The student chats

Sita asks: *"What is photosynthesis?"*

1. LessonForge works out what kind of request this is: a question, a request for a quiz, and so on.
2. It searches **only the shelves she's allowed**: the shared shelf plus School A's shelf. School B's materials are invisible to her, always, even if nothing else is found.
3. It looks at **Grade 8 material first**, because it knows her grade.
4. The AI writes the answer from those pages and shows **where each part came from**.
5. The conversation is saved **under her name only**. Nobody else can open it. To anyone else, it looks like it doesn't exist.

### She asks for a quiz

Sita says: *"Quiz me on this."*

- LessonForge makes a quiz from her textbook and opens it beside the chat.
- She answers the questions, gets her score, and can retry the ones she missed.

## Step 5: A teacher makes lesson plans

Mr. Ram, a Grade 10 Science teacher at School A, logs in the same way. His pass says **teacher**, so he sees all the tools.

- The lesson-plan form is **already filled in** with Grade 10, Science, because LessonForge knows what he teaches.
- He creates a lesson plan or a **multi-day unit**. The AI builds it from the curriculum, the textbook, and School A's own materials.
- He can **edit it, ask the AI to improve one part, and undo or redo** changes.
- He can **download** it as a Word document, slides, a worksheet, or a quiz.
- His plans are **private** at first. Only he can see them.

## Step 6: Teachers share with colleagues

Mr. Ram likes his plan, so he switches it to **"Share with my school"**.

- **Other teachers at School A** now see it in a **"Shared in my school"** list. They can **read it and make their own copy** to change for their class. They **can't change his original**.
- The copy remembers where it came from: *"Based on Mr. Ram's plan."*
- **Teachers at School B can't see it.**
- **Students never see lesson plans.**
- If he switches it back to **private**, it disappears from colleagues' lists immediately. Copies they already made stay theirs.

## Step 7: Everything is quietly noted for monitoring

Each meaningful action writes **one short line** in a logbook:

> *"A Grade 8 student at School A asked a Science question about Photosynthesis, answered from the textbook."*
> *"A student finished a quiz: 7 out of 10, in 4 minutes."*
> *"A teacher made a Grade 10 Science lesson plan and downloaded it."*
> *"A teacher shared a plan; another teacher copied it."*

**The actual words a child typed are never stored in this logbook**, only *what kind* of thing happened.

## Step 8: Edboox watches the numbers

Edboox can ask LessonForge, using its own special read-only pass:

- *How many students and teachers used it this week, per school?*
- *Which chapters are students asking about most?*
- *What's the average quiz score in Grade 8 Science, and which chapters are weakest?*
- *What has this particular student or teacher done this month?*
- *How much AI is each school using?*

Edboox can either get **ready-made summaries** or **collect every logbook line** for its own reports. If it misses a day, it picks up where it left off, so nothing is lost. A future school principal would only see **their own school**.

## Step 9: Keeping things safe and tidy

- **Old logbook lines are deleted** after about a year (a setting we can change).
- When a **student or teacher is removed**, their chats and plans are deleted. Their logbook lines lose the link to them, so school totals stay correct but nothing points back to the person.
- When a **school leaves**, its whole shelf can be removed in one go.
- The **record book** of who added books or became an owner is kept separately, for us only.
- If something goes wrong while writing a logbook line, the student **still gets their answer**. Logging never gets in the way.

## Today vs. later

**Today:**
- People use **our screens**.
- We use a **pretend Edboox** we build ourselves. It has its own pretend schools, teachers, and students, its own login page, and its own usage page. It works exactly the way the real one will.

**Later:**
- The **real Edboox** connects. We only change a few settings, like its address and keys, not the program.
- Edboox builds **its own screens** that use the very same LessonForge connection. Nothing changes on our side. A safety check stops us from accidentally breaking that connection.
- Our screens stay on as **our control room** for adding books.

**Changes already planned for, each needing only a small adjustment:**

| If we later want… | We… |
|---|---|
| Each school to have **only its own** library, no shared shelf | change one setting |
| A big school's library stored **completely separately** | flip a switch and move that school's shelf |
| School staff uploading **their own** materials | give them a "school admin" role that can only touch their school's shelf |
| Sharing plans with **specific teachers** or **editing together** | add that option on top of today's sharing |
| School-approved **model plans** the AI learns from | put them on that school's shelf |

## Where everything lives

| What | Where |
|---|---|
| Real accounts and passwords | **Edboox** only |
| Chats, quizzes, lesson plans, sharing, logbook, record book | **LessonForge's main storage** (Postgres) |
| The books, ready for searching | **LessonForge's search library** (Qdrant), organised by shelves |

## The whole thing in one breath

**We stock the library and put each book on the right shelf → a person logs into Edboox and gets a sealed pass → LessonForge checks it, decides what they may use, and learns their grade and school → students chat and take quizzes, teachers build, improve, download, and share lesson plans, each seeing only their school's shelves and their own or shared work → every action is noted without saving what anyone typed → Edboox reads the numbers to monitor usage → today on our screens with a pretend Edboox, later on Edboox's own screens with no change underneath.**
