"use strict";
/* LessonForge API client — loaded FIRST by every page.

   The one place that knows:
   - the API base (API) — every page calls lfFetch("/lessons/generate"), never a
     hard-coded "/api/v1/…", so a v2 move is one line;
   - the session: same-origin cookie + the X-LF-Request CSRF header the server
     requires on cookie-authenticated writes;
   - the error envelope {error:{code,message,details}} → lfErr();
   - auth: a 401 sends the user to Edboox to log in (the URL comes from the
     server, so pointing at the real Edboox is a server config change);
   - navigation: /me says what the caller may use; links they can't use are
     hidden and a page they can't use redirects to one they can. */

const API = "/api/v1";

function lfUrl(path) {
  return /^(https?:)?\/\//.test(path) || path.startsWith(API) ? path : API + path;
}

const LF_PAGES = ["/", "/units", "/library", "/chat", "/corpus"];

/* Resolves once the caller may use THIS page. On a page they can't use it
   redirects to one they can and never resolves — so no page load fires a doomed
   request. Every lfFetch (except /me itself) waits on it. */
let _lfGuard = null;
function lfGuard() {
  if (!_lfGuard) {
    _lfGuard = (async () => {
      const me = await lfMe();
      if (!me) return;                       // 401: lfFetch is already sending them to login
      const here = window.location.pathname;
      if (LF_PAGES.includes(here) && !me.nav.some((n) => n.path === here)) {
        if (me.nav.length) window.location.replace(me.nav[0].path);
        return new Promise(() => {});
      }
    })();
  }
  return _lfGuard;
}

let _lfRedirecting = false;
async function lfFetch(path, opt = {}) {
  if (lfUrl(path) !== API + "/me") await lfGuard();
  const headers = new Headers(opt.headers || {});
  headers.set("X-LF-Request", "1");
  const res = await fetch(lfUrl(path), { ...opt, headers, credentials: "same-origin" });
  if (res.status === 401 && !_lfRedirecting) {
    _lfRedirecting = true;
    let login = null;
    try { login = ((await res.clone().json()).error || {}).details?.login_url; } catch { /* no body */ }
    if (login) window.location.href = login;
  }
  return res;
}

/* Human-readable message from an error body (envelope or legacy shape). */
function lfErr(body, fallback = "request failed") {
  if (!body) return fallback;
  const e = body.error;
  if (e) {
    if (Array.isArray(e.details) && e.details.length && e.details[0].msg) {
      return e.message + ": " + e.details.slice(0, 3).map((d) => `${d.loc}: ${d.msg}`).join("; ");
    }
    return e.message || e.code || fallback;
  }
  return typeof body.detail === "string" ? body.detail : fallback;
}

/* who am I — cached for the page's lifetime */
let _lfMe = null;
async function lfMe() {
  if (_lfMe) return _lfMe;
  const res = await lfFetch("/me");
  if (!res.ok) return null;
  _lfMe = await res.json();
  return _lfMe;
}

function lfCan(perm) { return !!(_lfMe && _lfMe.permissions.includes(perm)); }

/* Hide nav links the caller can't use, show who is signed in, and move them off a
   page they have no access to (e.g. a student opening the lesson editor). */
async function lfBootNav() {
  await lfGuard();
  const me = await lfMe();
  if (!me) return;
  const allowed = new Set(me.nav.map((n) => n.path));
  document.querySelectorAll("a[href^='/']").forEach((a) => {
    const path = a.getAttribute("href");
    if (LF_PAGES.includes(path) && !allowed.has(path)) a.style.display = "none";
  });
  const bar = document.querySelector("header nav, header .nav, header") || document.body;
  const who = document.createElement("span");
  who.className = "lf-who";
  who.style.cssText = "margin-left:12px;font-size:13px;opacity:.85;white-space:nowrap";
  who.textContent = (me.name || me.subject) + " · " + me.role;
  const form = document.createElement("form");
  form.method = "POST"; form.action = "/auth/logout"; form.style.display = "inline";
  const btn = document.createElement("button");
  btn.type = "submit"; btn.className = "small ghost"; btn.textContent = "Sign out";
  btn.style.marginLeft = "8px";
  form.append(btn);
  bar.append(who, form);
}

document.addEventListener("DOMContentLoaded", () => { lfBootNav().catch(() => {}); });
