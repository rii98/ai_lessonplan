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

/* bind an <input>/<textarea> to obj[key], mutating in place (no re-render). */
function bound(obj, key, { type = "text", placeholder = "", area = false } = {}) {
  const el = area ? h("textarea", { placeholder }) : h("input", { type, placeholder });
  el.value = obj[key] ?? "";
  el.addEventListener("input", () => {
    if (type === "number") { const n = el.value.trim(); obj[key] = n === "" ? null : Number(n); }
    else obj[key] = el.value;
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
