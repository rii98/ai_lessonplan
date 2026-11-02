"""Fake Edboox — a dev-only stand-in for the real school platform.

It shares NOTHING with LessonForge but HTTP and the four documented contracts
(docs/EDBOOX_INTEGRATION.md): it must never import ``lessonforge``. It owns its
own database (``edboox``) and its own signing key, and it deliberately speaks in
Edboox-style shapes (``"Class 8"``, ``"Science and Technology"``, ``"nepali"``)
so LessonForge's anti-corruption mapper is exercised rather than bypassed.

Endpoints
  GET/POST /login                 username + password → Edboox session
  GET      /                      dashboard with "Open AI assistant"
  POST     /launch/lessonforge    60 s one-time token, auto-POSTed to LessonForge (§3a)
  POST     /api/ai-token          15-min bearer token for the logged-in user (§3b)
  GET      /.well-known/jwks.json public signing keys
  GET      /api/v1/ai/users/{sub} user-context projection (§6), X-Api-Key required
  GET      /usage                 pulls LessonForge's activity feed + usage report (§15)
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import os
import re
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import jwt
import psycopg
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Body, FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from psycopg.rows import dict_row

DB_URL = os.environ.get("EDBOOX_DATABASE_URL", "postgresql://edboox:edboox@localhost:5432/edboox")
ISSUER = os.environ.get("EDBOOX_ISSUER", "http://localhost:8200")
AUDIENCE = os.environ.get("LF_AUDIENCE", "lessonforge")
LF_PUBLIC_URL = os.environ.get("LF_PUBLIC_URL", "http://localhost:8000")   # browser-facing
LF_API_URL = os.environ.get("LF_API_URL", LF_PUBLIC_URL)                   # server-to-server
API_KEY = os.environ.get("EDBOOX_API_KEY", "dev-edboox-api-key")
DATA_DIR = Path(os.environ.get("EDBOOX_DATA_DIR", "/data"))
KID = os.environ.get("EDBOOX_KEY_ID", "edboox-dev-1")
SESSION_COOKIE = "edboox_session"
DEMO = os.environ.get("EDBOOX_DEMO", "false").lower() in ("1", "true", "yes")
HERE = Path(__file__).parent
LAUNCH_TTL_S, API_TTL_S = 60, 900


# ── keys ─────────────────────────────────────────────────────────────────────
def _load_or_create_key() -> rsa.RSAPrivateKey:
    path = DATA_DIR / "signing-key.pem"
    if path.exists():
        return serialization.load_pem_private_key(path.read_bytes(), password=None)  # type: ignore[return-value]
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                       serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption()))
    path.chmod(0o600)
    return key


def _session_secret() -> str:
    path = DATA_DIR / "session-secret"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(secrets.token_hex(32))
        path.chmod(0o600)
    return path.read_text().strip()


# ── database ─────────────────────────────────────────────────────────────────
SCHEMA = """
CREATE TABLE IF NOT EXISTS schools  (id text PRIMARY KEY, name text NOT NULL,
                                     district text, province text);
CREATE TABLE IF NOT EXISTS teachers (id serial PRIMARY KEY, school_id text REFERENCES schools(id),
                                     username text UNIQUE NOT NULL, password_hash text NOT NULL,
                                     full_name text NOT NULL, preferred_language text DEFAULT 'en');
CREATE TABLE IF NOT EXISTS students (id serial PRIMARY KEY, school_id text REFERENCES schools(id),
                                     username text UNIQUE NOT NULL, password_hash text NOT NULL,
                                     full_name text NOT NULL, grade text NOT NULL,
                                     section text, medium text, stream text,
                                     optional_subjects text[] NOT NULL DEFAULT '{}',
                                     preferred_language text DEFAULT 'ne');
CREATE TABLE IF NOT EXISTS teaching_assignments (
  teacher_id int REFERENCES teachers(id) ON DELETE CASCADE,
  grade text NOT NULL, section text NOT NULL DEFAULT '', subject text NOT NULL,
  PRIMARY KEY (teacher_id, grade, section, subject));
ALTER TABLE teachers ADD COLUMN IF NOT EXISTS is_admin boolean NOT NULL DEFAULT false;
"""


def _hash(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${salt.hex()}${digest.hex()}"


def _verify(password: str, stored: str) -> bool:
    try:
        _, salt, digest = stored.split("$")
        got = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1)
        return hmac.compare_digest(got.hex(), digest)
    except ValueError:
        return False


def _db():
    return psycopg.connect(DB_URL, row_factory=dict_row, autocommit=True)


def _seed() -> None:
    with _db() as conn:
        conn.execute(SCHEMA)
        # the platform owner is Edboox's admin too (manages students & teachers)
        conn.execute("UPDATE teachers SET is_admin = true WHERE username = 'owner'")
        if conn.execute("SELECT 1 FROM schools LIMIT 1").fetchone():
            return
        pw = _hash("password")
        conn.execute("INSERT INTO schools VALUES ('sch-7', 'Shree Janata Secondary School', "
                     "'Kaski', 'Gandaki'), ('sch-9', 'Himalaya Boarding School', 'Lalitpur', "
                     "'Bagmati')")
        teachers = [  # (school, username, name, lang) — teacher 1 is the owner (superadmin)
            ("sch-7", "owner", "Platform Owner", "en"),
            ("sch-7", "sita", "Sita Sharma", "ne"),
            ("sch-7", "ram", "Ram Thapa", "en"),
            ("sch-9", "maya", "Maya Gurung", "en"),
        ]
        for school, user, name, lang in teachers:
            conn.execute("INSERT INTO teachers (school_id, username, password_hash, full_name, "
                         "preferred_language) VALUES (%s, %s, %s, %s, %s)",
                         (school, user, pw, name, lang))
        conn.execute("""INSERT INTO teaching_assignments (teacher_id, grade, section, subject) VALUES
            (2, 'Class 8', 'A', 'Science and Technology'), (2, 'Class 10', 'A', 'Science and Technology'),
            (3, 'Class 8', 'B', 'Compulsory Mathematics'), (4, 'Class 10', '', 'Science and Technology')""")
        students = [  # school, username, name, grade, section, medium, optional
            ("sch-7", "aarav", "Aarav Karki", "Class 8", "A", "nepali", []),
            ("sch-7", "gita", "Gita Rai", "Class 10", "A", "english", ["Computer Science"]),
            ("sch-7", "bikash", "Bikash Magar", "Class 8", "B", "nepali", []),
            ("sch-9", "nima", "Nima Sherpa", "Class 10", "", "english", []),
        ]
        for school, user, name, grade, section, medium, optional in students:
            conn.execute("INSERT INTO students (school_id, username, password_hash, full_name, grade, "
                         "section, medium, optional_subjects) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                         (school, user, pw, name, grade, section, medium, optional))
        conn.execute("UPDATE teachers SET is_admin = true WHERE username = 'owner'")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.key = _load_or_create_key()
    app.state.session_secret = _session_secret()
    _seed()
    yield


app = FastAPI(title="Fake Edboox (dev)", lifespan=lifespan)


# ── identity helpers ─────────────────────────────────────────────────────────
def _user(sub: str) -> dict[str, Any] | None:
    kind, _, raw_id = sub.partition(":")
    if kind not in ("student", "teacher") or not raw_id.isdigit():
        return None
    table = "students" if kind == "student" else "teachers"
    with _db() as conn:
        row = conn.execute(f"SELECT * FROM {table} WHERE id = %s", (int(raw_id),)).fetchone()
    return {**row, "kind": kind, "sub": sub} if row else None


def _current(request: Request) -> dict[str, Any] | None:
    raw = request.cookies.get(SESSION_COOKIE)
    if not raw:
        return None
    try:
        sub = jwt.decode(raw, request.app.state.session_secret, algorithms=["HS256"])["sub"]
    except jwt.InvalidTokenError:
        return None
    return _user(sub)


def _mint(app_: FastAPI, user: dict[str, Any] | None, ttl: int, *, sub: str | None = None,
          role: str | None = None) -> str:
    now = int(time.time())
    claims: dict[str, Any] = {"iss": ISSUER, "aud": AUDIENCE, "iat": now, "exp": now + ttl,
                              "jti": uuid.uuid4().hex}
    if user is not None:
        claims |= {"sub": user["sub"], "role": user["kind"], "name": user["full_name"],
                   "school_id": user["school_id"]}
        if user["kind"] == "student":
            digits = "".join(ch for ch in user["grade"] if ch.isdigit())
            claims["grade"] = int(digits) if digits else None
    else:
        claims |= {"sub": sub, "role": role}
    return jwt.encode(claims, app_.state.key, algorithm="RS256", headers={"kid": KID})


# ── pages ────────────────────────────────────────────────────────────────────
_STYLE = """<style>body{font-family:system-ui,sans-serif;max-width:720px;margin:40px auto;padding:0 16px;
color:#1f2937}button,input{font:inherit;padding:8px 12px;border-radius:8px;border:1px solid #cbd5e1}
button{background:#2563eb;color:#fff;border:0;cursor:pointer}.card{border:1px solid #e5e7eb;
border-radius:12px;padding:20px;margin:16px 0}table{border-collapse:collapse;width:100%}
td,th{border-bottom:1px solid #e5e7eb;padding:6px;text-align:left;font-size:14px}.muted{color:#6b7280}
</style>"""


def _page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(f"<!doctype html><html><head><meta charset=utf-8><title>{title}</title>"
                        f"{_STYLE}</head><body><h1>📘 Edboox <small class=muted>(fake, dev)</small>"
                        f"</h1>{body}</body></html>")


@app.get("/login", response_class=HTMLResponse)
def login_form(error: str = "") -> HTMLResponse:
    msg = f"<p style='color:#b91c1c'>{html.escape(error)}</p>" if error else ""
    return _page("Edboox login", f"""<div class=card><h2>Sign in</h2>{msg}
      <form method=post action=/login><p><input name=username placeholder=username required></p>
      <p><input name=password type=password placeholder=password required></p>
      <button>Sign in</button></form>
      <p class=muted>Seed users (password <code>password</code>): owner, sita, ram (sch-7 teachers),
      maya (sch-9 teacher); aarav, gita, bikash (sch-7 students), nima (sch-9 student).</p></div>""")


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    with _db() as conn:
        for kind, table in (("teacher", "teachers"), ("student", "students")):
            row = conn.execute(f"SELECT id, password_hash FROM {table} WHERE username = %s",
                               (username,)).fetchone()
            if row and _verify(password, row["password_hash"]):
                resp = RedirectResponse("/", status_code=303)
                token = jwt.encode({"sub": f"{kind}:{row['id']}", "iat": int(time.time())},
                                   request.app.state.session_secret, algorithm="HS256")
                resp.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax")
                return resp
    return RedirectResponse("/login?error=wrong+username+or+password", status_code=303)


@app.get("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE)
    return resp


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    user = _current(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    extra = (f"Grade: {html.escape(user['grade'])}" if user["kind"] == "student"
             else "<a href=/usage>📈 AI usage report</a>")
    if user.get("is_admin"):
        extra += " · <a href=/admin>🏫 Manage students &amp; teachers</a>"
    if DEMO:
        extra += " · <a href=/demo>🔐 Live sign-in demo</a>"
    return _page("Edboox", f"""<div class=card><h2>Namaste, {html.escape(user['full_name'])}</h2>
      <p class=muted>{user['kind']} · school {html.escape(user['school_id'])} · {user['sub']}</p>
      <p>{extra}</p>
      <form method=post action=/launch/lessonforge><button>🤖 Open AI assistant</button></form>
      <p><a href=/logout>Sign out</a></p></div>""")


@app.post("/launch/lessonforge", response_class=HTMLResponse)
def launch(request: Request):
    """§3a: a 60 s one-time token, delivered in a POST body (never the URL)."""
    user = _current(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    token = _mint(request.app, user, LAUNCH_TTL_S)
    action = html.escape(f"{LF_PUBLIC_URL}/auth/launch")
    return HTMLResponse(f"""<!doctype html><html><body onload="document.forms[0].submit()">
      <form method=post action="{action}"><input type=hidden name=token value="{token}">
      <noscript><button>Continue to LessonForge</button></noscript></form></body></html>""")


@app.post("/api/ai-token")
def ai_token(request: Request):
    """§3b: what Edboox's own UI would use to call /api/v1 directly."""
    user = _current(request)
    if user is None:
        raise HTTPException(401, "not logged in")
    return {"token": _mint(request.app, user, API_TTL_S), "expires_in": API_TTL_S}


@app.get("/.well-known/jwks.json")
def jwks(request: Request):
    pub = jwt.algorithms.RSAAlgorithm.to_jwk(request.app.state.key.public_key(), as_dict=True)
    return {"keys": [{**pub, "kid": KID, "use": "sig", "alg": "RS256"}]}


@app.get("/api/v1/ai/users/{sub}")
def user_context(sub: str, x_api_key: str = Header(default="")):
    """§6 Contract 3: a read-only projection — only what the AI needs (no DOB,
    address, phone, guardians, photos, marks)."""
    if not hmac.compare_digest(x_api_key, API_KEY):
        raise HTTPException(401, "bad api key")
    user = _user(sub)
    if user is None:
        raise HTTPException(404, "unknown user")
    with _db() as conn:
        school = conn.execute("SELECT * FROM schools WHERE id = %s", (user["school_id"],)).fetchone()
        assignments = conn.execute(
            "SELECT grade, section, subject FROM teaching_assignments WHERE teacher_id = %s",
            (user["id"],)).fetchall() if user["kind"] == "teacher" else []
    student = None
    if user["kind"] == "student":
        student = {"grade": user["grade"], "section": user["section"] or None,
                   "medium": user["medium"], "stream": user["stream"],
                   "optional_subjects": list(user["optional_subjects"] or [])}
    return {
        "sub": sub, "role": user["kind"],
        "school": {"id": school["id"], "name": school["name"], "district": school["district"],
                   "province": school["province"]},
        "student": student,
        "teacher": ({"assignments": [{**a, "section": a["section"] or None,
                                      "class_id": f"{user['school_id']}-{a['grade']}-{a['section']}"}
                                     for a in assignments]}
                    if user["kind"] == "teacher" else None),
        "preferred_language": user["preferred_language"],
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


@app.get("/usage", response_class=HTMLResponse)
def usage(request: Request):
    """§15: pull LessonForge's feed and report with a SERVICE token, as the real
    Edboox would — proving the round trip end to end."""
    user = _current(request)
    if user is None or user["kind"] != "teacher":
        return RedirectResponse("/login", status_code=303)
    token = _mint(request.app, None, API_TTL_S, sub="service:edboox", role="service")
    headers = {"Authorization": f"Bearer {token}"}
    try:
        with httpx.Client(base_url=LF_API_URL, timeout=10) as client:
            report = client.get("/api/v1/reports/usage", headers=headers,
                                params={"group_by": "day,school,role"}).json()
            feed = client.get("/api/v1/events", headers=headers, params={"limit": 20}).json()
    except httpx.HTTPError as exc:
        return _page("Usage", f"<p>LessonForge unreachable: {html.escape(str(exc))}</p>")
    rows = "".join(
        f"<tr><td>{r.get('day')}</td><td>{html.escape(str(r.get('school')))}</td>"
        f"<td>{r.get('role')}</td><td>{r['active_users']}</td><td>{r['chat_messages']}</td>"
        f"<td>{r['quizzes_created']}</td><td>{r['lessons']}</td>"
        f"<td>{r['tokens_in']}/{r['tokens_out']}</td></tr>" for r in report.get("rows", []))
    events = "".join(
        f"<tr><td>{e['id']}</td><td>{e['type']}</td><td>{html.escape(str(e['subject']))}</td>"
        f"<td>{e['tenant_id']}</td><td><code>{html.escape(json.dumps(e['props']))[:160]}</code></td></tr>"
        for e in feed.get("items", []))
    return _page("AI usage", f"""<p><a href=/>← back</a></p><div class=card><h2>Usage (30 days)</h2>
      <table><tr><th>day</th><th>school</th><th>role</th><th>active</th><th>chats</th>
      <th>quizzes</th><th>lessons</th><th>tokens in/out</th></tr>{rows}</table></div>
      <div class=card><h2>Latest events (content-free)</h2><table><tr><th>#</th><th>type</th>
      <th>subject</th><th>school</th><th>props</th></tr>{events}</table></div>""")


@app.get("/health")
def health():
    return JSONResponse({"status": "ok"})


# ── Edboox admin: add / remove students and teachers ─────────────────────────
# Edboox is the system of record for people; LessonForge only ever sees them
# through signed tokens and the context projection.
def _require_admin(request: Request) -> dict[str, Any]:
    user = _current(request)
    if user is None or not user.get("is_admin"):
        raise HTTPException(403, "Edboox admins only")
    return user


def _opts(values: list[str], selected: str | None = None) -> str:
    return "".join(f"<option{' selected' if v == selected else ''}>{html.escape(v)}</option>"
                   for v in values)


def _remove_btn(kind: str, pid: int, what: str) -> str:
    return (f"<form method=post action=/admin/{kind}/{pid}/delete "
            f"onsubmit=\"return confirm('Remove this {what} from Edboox?')\">"
            f"<button style='background:#b91c1c'>Remove</button></form>")


@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request, msg: str = ""):
    _require_admin(request)
    with _db() as conn:
        schools = conn.execute("SELECT * FROM schools ORDER BY id").fetchall()
        students = conn.execute("SELECT * FROM students ORDER BY school_id, grade, id").fetchall()
        teachers = conn.execute("SELECT * FROM teachers ORDER BY school_id, id").fetchall()
        assign = conn.execute("SELECT * FROM teaching_assignments ORDER BY teacher_id").fetchall()
    members: dict[str, list[int]] = {sc["id"]: [0, 0] for sc in schools}
    for t in teachers:
        members.setdefault(t["school_id"], [0, 0])[0] += 1
    for st in students:
        members.setdefault(st["school_id"], [0, 0])[1] += 1
    by_t: dict[int, list[str]] = {}
    for a in assign:
        by_t.setdefault(a["teacher_id"], []).append(
            f"{a['grade']}{' ' + a['section'] if a['section'] else ''} · {a['subject']}")
    school_ids = [sc["id"] for sc in schools]
    grades = [f"Class {n}" for n in range(1, 13)]
    e = html.escape
    srows = "".join(
        f"<tr><td>student:{r['id']}</td><td>{e(r['full_name'])}</td><td>{e(r['username'])}</td>"
        f"<td>{r['school_id']}</td><td>{e(r['grade'])} {e(r['section'] or '')}</td>"
        f"<td>{e(r['medium'] or '')}</td><td>{_remove_btn('students', r['id'], 'student')}</td></tr>"
        for r in students)
    trows = "".join(
        f"<tr><td>teacher:{t['id']}</td><td>{e(t['full_name'])}"
        f"{' <b>(admin)</b>' if t['is_admin'] else ''}</td><td>{e(t['username'])}</td>"
        f"<td>{t['school_id']}</td><td>{'<br>'.join(e(x) for x in by_t.get(t['id'], []))}</td>"
        f"<td>{'' if t['is_admin'] else _remove_btn('teachers', t['id'], 'teacher')}</td></tr>"
        for t in teachers)
    schrows = "".join(
        f"<tr><td>{e(sc['id'])}</td><td>{e(sc['name'])}</td><td>{e(sc['district'] or '')}</td>"
        f"<td>{e(sc['province'] or '')}</td><td>{members[sc['id']][0]}</td><td>{members[sc['id']][1]}</td>"
        f"<td>{'' if sum(members[sc['id']]) else _remove_school_btn(sc['id'])}</td></tr>"
        for sc in schools)
    note = f"<p style='color:#047857'><b>{e(msg)}</b></p>" if msg else ""
    return _page("Edboox admin", f"""<p><a href=/>← back</a></p>{note}
      <div class=card><h2>Schools</h2><table><tr><th>School id</th><th>Name</th><th>District</th>
      <th>Province</th><th>Teachers</th><th>Students</th><th></th></tr>{schrows}</table>
      <h3>Add a school</h3><form method=post action=/admin/schools>
      <p><input name=id placeholder="School id (sch-12)" required pattern="[A-Za-z0-9][A-Za-z0-9_.\\-]{{0,63}}"
        title="Letters, digits, dot, dash or underscore; no spaces">
      <input name=name placeholder="School name" required size=34></p>
      <p><input name=district placeholder=District> <input name=province placeholder=Province>
      <button>Add school</button></p>
      <p class=muted>The id is what LessonForge sees in the sign-in token: it separates each school's
      shared plans and private knowledge shelf. It can't be changed later. A school can be removed
      only while it has no teachers or students.</p></form></div>
      <div class=card><h2>Students</h2><table><tr><th>Edboox id</th><th>Name</th><th>Username</th>
      <th>School</th><th>Class</th><th>Medium</th><th></th></tr>{srows}</table>
      <h3>Add a student</h3><form method=post action=/admin/students>
      <p><input name=full_name placeholder="Full name" required> <input name=username placeholder=username required>
      <input name=password placeholder=password value=password required></p>
      <p><select name=school_id>{_opts(school_ids)}</select> <select name=grade>{_opts(grades, 'Class 8')}</select>
      <input name=section placeholder="Section (A)" size=6>
      <select name=medium>{_opts(['nepali', 'english'])}</select> <button>Add student</button></p></form></div>
      <div class=card><h2>Teachers</h2><table><tr><th>Edboox id</th><th>Name</th><th>Username</th>
      <th>School</th><th>Teaches</th><th></th></tr>{trows}</table>
      <h3>Add a teacher</h3><form method=post action=/admin/teachers>
      <p><input name=full_name placeholder="Full name" required> <input name=username placeholder=username required>
      <input name=password placeholder=password value=password required>
      <select name=school_id>{_opts(school_ids)}</select></p>
      <p><textarea name=assignments rows=3 cols=60
        placeholder="One per line: Class 8 | A | Science and Technology"></textarea></p>
      <button>Add teacher</button></form></div>
      <p class=muted>Removing someone here removes them from Edboox only. To delete what LessonForge
      holds about them, use LessonForge's Owner console → People → Delete their data.</p>""")


_SCHOOL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")   # = LessonForge's school id rule


def _remove_school_btn(sid: str) -> str:
    return (f"<form method=post action=/admin/schools/{html.escape(sid)}/delete "
            f"onsubmit=\"return confirm('Remove this school from Edboox?')\">"
            f"<button style='background:#b91c1c'>Remove</button></form>")


@app.post("/admin/schools")
def add_school(request: Request, id: str = Form(...), name: str = Form(...),
               district: str = Form(""), province: str = Form("")):
    _require_admin(request)
    sid, name = id.strip(), name.strip()
    if not _SCHOOL_ID.match(sid) or not name:
        return RedirectResponse("/admin?msg=School+id+must+be+letters,+digits,+.+-+_+(no+spaces)",
                                status_code=303)
    try:
        with _db() as conn:
            conn.execute("INSERT INTO schools (id, name, district, province) VALUES (%s,%s,%s,%s)",
                         (sid, name, district.strip() or None, province.strip() or None))
    except psycopg.errors.UniqueViolation:
        return RedirectResponse("/admin?msg=That+school+id+is+taken", status_code=303)
    return RedirectResponse(f"/admin?msg=Added+school+{sid}", status_code=303)


@app.post("/admin/schools/{sid}/delete")
def remove_school(request: Request, sid: str):
    _require_admin(request)
    try:
        with _db() as conn:
            conn.execute("DELETE FROM schools WHERE id = %s", (sid,))
    except psycopg.errors.ForeignKeyViolation:
        return RedirectResponse("/admin?msg=Remove+its+teachers+and+students+first", status_code=303)
    return RedirectResponse(f"/admin?msg=Removed+school+{sid}", status_code=303)


@app.post("/admin/students")
def add_student(request: Request, full_name: str = Form(...), username: str = Form(...),
                password: str = Form(...), school_id: str = Form(...), grade: str = Form(...),
                section: str = Form(""), medium: str = Form("nepali")):
    _require_admin(request)
    try:
        with _db() as conn:
            row = conn.execute(
                "INSERT INTO students (school_id, username, password_hash, full_name, grade, "
                "section, medium) VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                (school_id, username.strip(), _hash(password), full_name.strip(), grade,
                 section.strip() or None, medium)).fetchone()
    except psycopg.errors.UniqueViolation:
        return RedirectResponse("/admin?msg=That+username+is+taken", status_code=303)
    return RedirectResponse(f"/admin?msg=Added+student:{row['id']}", status_code=303)


@app.post("/admin/teachers")
def add_teacher(request: Request, full_name: str = Form(...), username: str = Form(...),
                password: str = Form(...), school_id: str = Form(...),
                assignments: str = Form("")):
    _require_admin(request)
    try:
        with _db() as conn, conn.transaction():
            tid = conn.execute(
                "INSERT INTO teachers (school_id, username, password_hash, full_name) "
                "VALUES (%s,%s,%s,%s) RETURNING id",
                (school_id, username.strip(), _hash(password), full_name.strip())).fetchone()["id"]
            for line in assignments.splitlines():
                parts = [x.strip() for x in line.split("|")]
                if len(parts) == 3 and parts[0] and parts[2]:
                    conn.execute("INSERT INTO teaching_assignments VALUES (%s,%s,%s,%s) "
                                 "ON CONFLICT DO NOTHING", (tid, parts[0], parts[1], parts[2]))
    except psycopg.errors.UniqueViolation:
        return RedirectResponse("/admin?msg=That+username+is+taken", status_code=303)
    return RedirectResponse(f"/admin?msg=Added+teacher:{tid}", status_code=303)


@app.post("/admin/{kind}/{pid}/delete")
def remove_person(request: Request, kind: str, pid: int):
    _require_admin(request)
    if kind not in ("students", "teachers"):
        raise HTTPException(404)
    with _db() as conn:
        guard = " AND NOT is_admin" if kind == "teachers" else ""
        conn.execute(f"DELETE FROM {kind} WHERE id = %s{guard}", (pid,))
    return RedirectResponse(f"/admin?msg=Removed+{kind[:-1]}:{pid}", status_code=303)


# ── live sign-in demo (EDBOOX_DEMO, off by default) ──────────────────────────
def _demo_on() -> None:
    if not DEMO:
        raise HTTPException(404, "demo is off (set LF_DEMO=true)")


@app.get("/demo", response_class=HTMLResponse)
def demo_page():
    if not DEMO:
        return _page("Demo off", "<div class=card><h2>The live sign-in demo is off</h2>"
                     "<p>Start the stack with <code>LF_DEMO=true docker compose --profile dev "
                     "up -d</code> to switch it on (in both apps).</p></div>")
    with _db() as conn:
        users = ([{"u": r["username"], "label": f"{r['full_name']} — student, {r['grade']}, "
                                                f"{r['school_id']}"}
                  for r in conn.execute("SELECT * FROM students ORDER BY id").fetchall()]
                 + [{"u": r["username"], "label": f"{r['full_name']} — teacher, {r['school_id']}"}
                    for r in conn.execute("SELECT * FROM teachers ORDER BY id").fetchall()])
    page = (HERE / "demo.html").read_text(encoding="utf-8")
    return HTMLResponse(page.replace("__LF_URL__", LF_PUBLIC_URL)
                        .replace("__USERS__", json.dumps(users)))


@app.post("/demo/api/login")
def demo_login(request: Request, body: dict = Body(...)):
    _demo_on()
    username, password = str(body.get("username", "")), str(body.get("password", ""))
    with _db() as conn:
        for kind, table in (("teacher", "teachers"), ("student", "students")):
            row = conn.execute(f"SELECT * FROM {table} WHERE username = %s", (username,)).fetchone()
            if row and _verify(password, row["password_hash"]):
                sub = f"{kind}:{row['id']}"
                resp = JSONResponse({"sub": sub, "name": row["full_name"], "role": kind,
                                     "school_id": row["school_id"], "grade": row.get("grade"),
                                     "password_check": "scrypt hash matched — inside Edboox only"})
                resp.set_cookie(SESSION_COOKIE, jwt.encode(
                    {"sub": sub, "iat": int(time.time())}, request.app.state.session_secret,
                    algorithm="HS256"), httponly=True, samesite="lax")
                return resp
    raise HTTPException(401, "wrong username or password")


@app.post("/demo/api/pass")
def demo_pass(request: Request):
    """Exactly what 'Open AI assistant' mints, returned as JSON instead of a form."""
    _demo_on()
    user = _current(request)
    if user is None:
        raise HTTPException(401, "sign in first")
    token = _mint(request.app, user, LAUNCH_TTL_S)
    return {"token": token, "header": jwt.get_unverified_header(token),
            "claims": jwt.decode(token, options={"verify_signature": False}),
            "signed_with": f"Edboox's PRIVATE key '{KID}' (it never leaves Edboox)"}


@app.get("/demo/api/projection")
def demo_projection(request: Request):
    """What Edboox answers when LessonForge asks about this user (Contract 3)."""
    _demo_on()
    user = _current(request)
    if user is None:
        raise HTTPException(401, "sign in first")
    return user_context(user["sub"], x_api_key=API_KEY)
