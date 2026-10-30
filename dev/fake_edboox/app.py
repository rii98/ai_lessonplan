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
from fastapi import FastAPI, Form, Header, HTTPException, Request
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
