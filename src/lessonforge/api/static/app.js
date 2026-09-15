"use strict";
/* Shared browser helpers for the plain-HTML pages (editor, units, library).
   No framework, no build step — same idioms as the original lesson editor. */

const $ = (id) => document.getElementById(id);

/* fetch wrapper: returns the Response; callers decide how to read it. */
async function api(method, url, body) {
  const opt = { method, headers: {} };
  if (body !== undefined) {
    opt.headers["Content-Type"] = "application/json";
    opt.body = JSON.stringify(body);
  }
  return fetch(url, opt);
}

/* JSON helper: throws with the server's detail on a non-2xx. */
async function apiJSON(method, url, body) {
  const res = await api(method, url, body);
  let data = null;
  try { data = await res.json(); } catch { /* empty body */ }
  if (!res.ok) {
    const detail = data && (data.detail || data.message);
    throw new Error(typeof detail === "string" ? detail : `${res.status} ${res.statusText}`);
  }
  return data;
}

/* hyperscript: h("div", {class:"x", onclick:fn}, child, ...) */
function h(tag, attrs, ...kids) {
  const e = document.createElement(tag);
  for (const k in (attrs || {})) {
    if (k === "class") e.className = attrs[k];
    else if (k === "html") e.innerHTML = attrs[k];
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), attrs[k]);
    else if (attrs[k] != null && attrs[k] !== false) e.setAttribute(k, attrs[k]);
  }
  for (const kid of kids.flat()) {
    if (kid == null || kid === false) continue;
    e.append(kid.nodeType ? kid : document.createTextNode(kid));
  }
  return e;
}

function field(labelText, control) {
  return h("label", { class: "f" }, h("span", {}, labelText), control);
}

/* bind an <input>/<textarea> to obj[key], mutating in place (no re-render).
   Textareas auto-grow to fit their content so long text wraps instead of being
   clipped in a fixed box. */
function bound(obj, key, { type = "text", placeholder = "", area = false } = {}) {
  const el = area ? h("textarea", { class: "grow", rows: "1", placeholder }) : h("input", { type, placeholder });
  el.value = obj[key] ?? "";
  const grow = area ? () => { el.style.height = "auto"; el.style.height = (el.scrollHeight + 2) + "px"; } : null;
  if (grow) setTimeout(grow, 0);
  el.addEventListener("input", () => {
    if (type === "number") { const n = el.value.trim(); obj[key] = n === "" ? null : Number(n); }
    else obj[key] = el.value;
    if (grow) grow();
  });
  return el;
}

/* editable list of strings; mutates `arr` in place; `rerender` redraws the parent
   when the list's structure (add/remove) changes. */
function stringList(arr, placeholder, rerender) {
  const box = h("div", {});
  arr.forEach((_, i) => {
    const inp = h("input", { placeholder });
    inp.value = arr[i];
    inp.addEventListener("input", () => { arr[i] = inp.value; });
    box.append(h("div", { class: "row", style: "margin-bottom:6px" }, inp,
      h("button", { class: "small ghost", onclick: () => { arr.splice(i, 1); rerender(); } }, "✕")));
  });
  box.append(h("button", { class: "small", onclick: () => { arr.push(""); rerender(); } }, "+ add"));
  return box;
}

const truncate = (s, n) => { s = String(s || "").trim(); return s.length > n ? s.slice(0, n - 1) + "…" : s; };
const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;

/* A collapsible section (progressive disclosure). `cfg`:
   {icon, title, summary, ok (bool|undefined), open (bool), onToggle(open), actions:[el]}
   `buildBody(bodyEl)` fills the body lazily when the section is (or becomes) open.
   Action buttons in the header don't toggle the section. */
function collapsible(cfg, buildBody) {
  const sec = h("div", { class: "ed-sec" + (cfg.open ? " open" : "") });
  const body = h("div", { class: "ed-body" });
  (cfg.actions || []).forEach((a) => a.addEventListener("click", (e) => e.stopPropagation()));
  const head = h("div", { class: "ed-head", role: "button", tabindex: "0" },
    h("div", { class: "ed-head-l" },
      h("span", { class: "ed-chev" }, "▾"),
      cfg.icon != null ? h("span", { class: "ed-icon" }, cfg.icon) : null,
      h("span", { class: "ed-title" }, cfg.title),
      cfg.ok === undefined ? null : h("span", { class: "ed-dot " + (cfg.ok ? "ok" : "warn"),
        title: cfg.ok ? "looks complete" : "needs attention" })),
    h("div", { class: "ed-head-r" },
      cfg.summary ? h("span", { class: "ed-sum" }, cfg.summary) : null,
      ...(cfg.actions || [])));
  let isOpen = !!cfg.open;
  const fill = () => { body.textContent = ""; buildBody(body); };
  const toggle = () => {
    isOpen = !isOpen;
    if (isOpen) { sec.classList.add("open"); fill(); }
    else { sec.classList.remove("open"); body.textContent = ""; }
    if (cfg.onToggle) cfg.onToggle(isOpen);
  };
  head.addEventListener("click", toggle);
  head.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); } });
  if (isOpen) fill();
  sec.append(head, body);
  return sec;
}

/* tag-style editor for a list of SHORT strings (materials, tags…). Mutates `arr`
   in place and manages its own DOM — no parent re-render, so scroll/focus survive
   typing. Enter adds, Backspace-on-empty pops. `onChange` (optional) fires after
   any add/remove/edit, e.g. to recompute a live derived view. */
function chipInput(arr, placeholder, onChange) {
  const wrap = h("div", { class: "chips" });
  const changed = () => { if (onChange) onChange(); };
  const build = () => {
    wrap.textContent = "";
    arr.forEach((_, i) => {
      const inp = h("input", { class: "chip-inp filled" });
      inp.value = arr[i];
      const size = () => { inp.style.width = Math.max(40, (inp.value.length || 4) * 7.6 + 8) + "px"; };
      size();
      inp.addEventListener("input", () => { arr[i] = inp.value; size(); changed(); });
      const x = h("button", { class: "chip-x", type: "button", title: "Remove",
        onclick: () => { arr.splice(i, 1); build(); changed(); } }, "×");
      wrap.append(h("span", { class: "chip-ed" }, inp, x));
    });
    const add = h("input", { class: "chip-inp add", placeholder });
    const commit = (keepFocus) => {
      const v = add.value.trim();
      if (!v) return;
      arr.push(v); build(); changed();
      if (keepFocus) wrap.querySelector(".chip-inp.add").focus();
    };
    add.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); commit(true); }
      else if (e.key === "Backspace" && add.value === "" && arr.length) { arr.pop(); build(); changed(); wrap.querySelector(".chip-inp.add").focus(); }
    });
    add.addEventListener("blur", () => commit(false));
    wrap.append(add);
  };
  build();
  return wrap;
}

/* stacked editor for a list of SENTENCE-length strings (activities, seeds,
   instructions). Each row is a full-width auto-growing textarea so long text
   wraps instead of overflowing a chip. Mutates `arr` in place; self-managed.
   `onChange` (optional) fires after any add/remove/edit. */
function lineList(arr, placeholder, onChange) {
  const wrap = h("div", { class: "linelist" });
  const changed = () => { if (onChange) onChange(); };
  const build = () => {
    wrap.textContent = "";
    arr.forEach((_, i) => {
      const ta = h("textarea", { class: "line-inp", rows: "1", placeholder });
      ta.value = arr[i];
      const grow = () => { ta.style.height = "auto"; ta.style.height = (ta.scrollHeight + 2) + "px"; };
      ta.addEventListener("input", () => { arr[i] = ta.value; grow(); changed(); });
      setTimeout(grow, 0);
      const x = h("button", { class: "line-x", type: "button", title: "Remove",
        onclick: () => { arr.splice(i, 1); build(); changed(); } }, "×");
      wrap.append(h("div", { class: "line-row" }, ta, x));
    });
    wrap.append(h("button", { class: "line-add", type: "button",
      onclick: () => { arr.push(""); build(); changed(); const t = wrap.querySelectorAll(".line-inp"); t[t.length - 1].focus(); } },
      "+ Add"));
  };
  build();
  return wrap;
}

/* status line: setStatus(el, "working…", "busy" | "err" | "ok" | "") */
function setStatus(el, text, kind = "") {
  if (!el) return;
  el.className = "status" + (kind === "err" ? " err" : "");
  el.textContent = "";
  if (kind === "busy") el.append(h("span", { class: "spinner" }), " " + (text || "Working…"));
  else el.textContent = text || "";
}

/* Tidy a heading_path breadcrumb ("A > **B** > C") into "A › B › C". */
function prettyBreadcrumb(s) {
  return String(s || "").replace(/\*\*/g, "").replace(/\s*>\s*/g, " › ").trim();
}

/* One retrieved chunk: a click-to-expand row showing its full text on demand. */
function groundingChunkRow(ch) {
  const label = prettyBreadcrumb(ch.heading_path) || prettyBreadcrumb(ch.chapter) || `chunk ${ch.chunk_index ?? "?"}`;
  const d = h("details", { class: "gchunk" });
  d.append(h("summary", {}, label, h("span", { class: "muted" }, ` · ${ch.score}`)));
  d.append(h("div", { class: "gchunk-text" }, ch.text || "(no text available)"));
  return d;
}

/* Render a grounding provenance tree (from POST /grounding/preview) into `el`:
   which chunks grounded a lesson, by collection → document → heading hierarchy.
   Identity + hierarchy only, never the quoted text. */
function renderGroundingTree(el, tree) {
  el.textContent = "";
  if (!tree || !tree.grounded) {
    el.append(h("p", { class: "sub" },
      "No reference material matched this topic — generated from the model's own curriculum knowledge."));
    return;
  }
  const n = (tree.sources || []).length;
  el.append(h("p", { class: "sub" },
    `Grounded in ${n} source${n === 1 ? "" : "s"}. These are the retrieved chunks (looked up live for this topic — the hierarchy, not the quoted text).`));
  for (const col of tree.collections) {
    el.append(h("div", { class: "row", style: "gap:6px;margin:10px 0 2px" },
      h("span", { class: "tag" + (col.authoritative ? " unit" : "") }, col.name),
      col.authoritative ? h("span", { class: "sub", style: "margin:0" }, "authoritative") : null,
      h("span", { class: "sub", style: "margin:0" }, `· ${col.count} chunk${col.count === 1 ? "" : "s"}`)));
    for (const doc of col.documents) {
      const box = h("div", { style: "margin:2px 0 6px 2px" },
        h("div", { style: "font-weight:600;font-size:13px" }, doc.source));
      doc.chunks.forEach((ch) => box.append(groundingChunkRow(ch)));
      el.append(box);
    }
  }
}

/* Fetch the grounding tree for a topic and render it into `el` (with a spinner). */
async function loadGrounding(el, { topic, grade, subject, framework }) {
  if (!topic) return;
  el.textContent = "";
  el.append(h("p", { class: "sub" }, h("span", { class: "spinner" }), " Looking up sources…"));
  try {
    const tree = await apiJSON("POST", "/grounding/preview", { topic, grade, subject, framework });
    renderGroundingTree(el, tree);
  } catch (e) {
    el.textContent = "";
    el.append(h("p", { class: "status err" }, "Couldn't load sources: " + e.message));
  }
}

function fmtDate(iso) {
  if (!iso) return "";
  try { return new Date(iso).toLocaleString(); } catch { return iso; }
}

/* Shared top bar + tab nav, rendered into <header class="top"> by data-page. */
const NAV = [
  { href: "/", label: "Lesson", key: "lesson" },
  { href: "/units", label: "Units", key: "units" },
  { href: "/library", label: "Library", key: "library" },
  { href: "/chat", label: "Chat", key: "chat" },
  { href: "/corpus", label: "Knowledge base", key: "corpus" },
];
function mountNav(active, title, subtitle) {
  const header = document.querySelector("header.top");
  if (!header) return;
  header.textContent = "";
  header.append(
    h("div", { class: "brand" },
      h("h1", {}, title),
      subtitle ? h("p", {}, subtitle) : null),
    h("nav", { class: "tabs" },
      ...NAV.map((n) => h("a", { href: n.href, class: n.key === active ? "active" : "" }, n.label)))
  );
}
