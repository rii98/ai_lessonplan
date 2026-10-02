/* Chat artifacts — the side panel and the interactive quiz player.
 *
 * Generic on purpose: `ChatArtifacts.open(id)` loads an artifact and hands it to the
 * renderer registered for its `kind` (`RENDERERS`). A quiz is the first kind; a
 * flashcard deck or study plan would add one entry and its own view code — the card,
 * the panel, persistence and history are shared.
 *
 * Needs from the host page (chat.html), via `ChatArtifacts.init({...})`:
 *   body, titleEl      the panel's content / title elements
 *   api(method,url,b)  JSON fetch helper that throws on !ok
 *   renderRich(text)   Markdown + math → an element (so questions/explanations render LaTeX)
 *   onChange(summary)  called when a quiz's attempts change (to refresh its card)
 *   onNew(summary)     called when a new artifact appears (retry-missed)
 *   onDeleted(id)      called after a delete
 *   toast(msg)
 */
(function () {
  "use strict";
  const A = (window.ChatArtifacts = {});
  let C = null;
  const RENDERERS = { quiz: { open: openQuiz } };

  function h(tag, attrs, ...kids) {
    const e = document.createElement(tag);
    for (const k in attrs || {}) {
      if (k === "class") e.className = attrs[k];
      else if (k.startsWith("on")) e.addEventListener(k.slice(2), attrs[k]);
      else if (attrs[k] != null && attrs[k] !== false) e.setAttribute(k, attrs[k]);
    }
    for (const kid of kids.flat()) if (kid != null && kid !== false) e.append(kid.nodeType ? kid : document.createTextNode(kid));
    return e;
  }
  const rich = (t) => C.renderRich(t || "");
  const btn = (label, cls, fn) => h("button", { class: "btn " + (cls || ""), type: "button", onclick: fn }, label);
  const pct = (a, b) => (b ? Math.round((100 * a) / b) : 0);
  const fmtScore = (s) => (Number.isInteger(s) ? String(s) : s.toFixed(1));
  const when = (iso) => new Date(iso).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });

  A.init = (opts) => { C = opts; };

  // ── generic entry ──────────────────────────────────────────────────────────
  A.open = async (id) => {
    try {
      const d = await C.api("GET", `/chat/artifacts/${id}`);
      const r = RENDERERS[d.summary.kind];
      if (!r) throw new Error("This kind of item can't be opened yet.");
      r.open(d);
    } catch (e) { C.toast(e.message || "Couldn't open it."); }
  };
  A.kinds = () => Object.keys(RENDERERS);

  // ── quiz ───────────────────────────────────────────────────────────────────
  const S = { id: null, summary: null, play: null, attempts: [], attemptId: null, idx: 0, results: {} };
  const root = () => C.body;
  const mount = (...nodes) => { root().replaceChildren(h("div", { class: "qz" }, ...nodes)); root().scrollTop = 0; };

  function openQuiz(d) {
    Object.assign(S, { id: d.summary.id, summary: d.summary, play: d.play, attempts: d.attempts,
      attemptId: null, idx: 0, results: {} });
    C.titleEl.textContent = d.play.title;
    intro();
  }

  async function refreshSummary() {
    const d = await C.api("GET", `/chat/artifacts/${S.id}`);
    S.summary = d.summary; S.attempts = d.attempts;
    C.onChange(d.summary);
  }

  function intro() {
    const p = S.play, n = p.questions.length;
    const kinds = {};
    p.questions.forEach((q) => (kinds[q.type] = (kinds[q.type] || 0) + 1));
    const label = (t) => t.replace(/_/g, " ");
    const nodes = [
      h("h2", {}, p.title),
      h("p", { class: "sub" }, `${n} question${n === 1 ? "" : "s"} · I check each answer as you go and explain it.`),
      h("div", { class: "chips" }, Object.entries(kinds).map(([t, c]) => h("span", { class: "chip" }, `${c} ${label(t)}`))),
    ];
    (p.notes || []).forEach((m) => nodes.push(h("p", { class: "sub" }, "ⓘ " + m)));
    const row = h("div", { class: "row" });
    if (S.summary.in_progress) row.append(btn("Resume", "primary", () => resume(S.summary.in_progress)));
    row.append(btn(S.summary.in_progress ? "Start over" : "Start quiz", S.summary.in_progress ? "" : "primary", startAttempt));
    row.append(exportMenu(), btn("Delete", "small", deleteQuiz));
    nodes.push(row);
    if (S.attempts.length) {
      nodes.push(h("div", { class: "sect" }, "History"));
      S.attempts.forEach((a) => nodes.push(h("div", { class: "att", onclick: () => reviewAttempt(a.id) },
        h("span", {}, when(a.created_at)),
        h("span", { class: "muted" }, a.status === "completed" ? "completed" : `${a.answered}/${a.total} answered`),
        h("span", { class: "sc" }, `${fmtScore(a.score)} / ${a.total}`))));
    }
    if ((p.sources || []).length) nodes.push(h("div", { class: "src" }, "Based on: " + p.sources.join(" · ")));
    mount(...nodes);
  }

  function exportMenu() {
    const sel = h("select", { class: "pick", style: "width:auto;padding:5px 8px;font-size:12px" },
      ["docx", "pptx", "md"].map((f) => h("option", { value: f }, "Download " + f)));
    const go = btn("⬇", "small", async () => {
      try {
        const r = await fetch(`/chat/artifacts/${S.id}/export?fmt=${sel.value}`, { method: "POST" });
        if (!r.ok) throw new Error("export failed");
        const blob = await r.blob();
        const m = /filename="?([^"]+)"?/.exec(r.headers.get("Content-Disposition") || "");
        const a = h("a", { href: URL.createObjectURL(blob), download: m ? m[1] : "quiz." + sel.value });
        document.body.append(a); a.click(); a.remove();
      } catch (e) { C.toast(e.message); }
    });
    return h("span", { class: "row", style: "margin:0;gap:4px" }, sel, go);
  }

  async function deleteQuiz() {
    if (!confirm("Delete this quiz and its history?")) return;
    try { await C.api("DELETE", `/chat/artifacts/${S.id}`); C.onDeleted(S.id); }
    catch (e) { C.toast(e.message); }
  }

  async function startAttempt() {
    try {
      const r = await C.api("POST", `/chat/artifacts/${S.id}/attempts`);
      Object.assign(S, { attemptId: r.attempt_id, idx: 0, results: {} });
      question();
    } catch (e) { C.toast(e.message); }
  }

  async function resume(attemptId) {
    try {
      const rv = await C.api("GET", `/chat/attempts/${attemptId}`);
      S.attemptId = attemptId; S.results = {};
      rv.questions.forEach((q) => { if (q.answered) S.results[q.id] = q; });
      const next = S.play.questions.findIndex((q) => !S.results[q.id]);
      S.idx = next < 0 ? S.play.questions.length - 1 : next;
      question();
    } catch (e) { C.toast(e.message); }
  }

  // ── a question ─────────────────────────────────────────────────────────────
  function question() {
    const qs = S.play.questions, q = qs[S.idx], n = qs.length;
    const done = Object.keys(S.results).length;
    const slot = h("div", {});
    const fb = h("div", {});
    mount(
      h("div", { class: "prog" }, h("span", {}, `Question ${S.idx + 1} of ${n}`),
        h("div", { class: "bar" }, h("i", { style: `width:${pct(done, n)}%` })),
        btn("✕ Exit", "small", exitQuiz)),
      h("div", { class: "qtext" }, rich(q.prompt)),
      slot, fb);
    const ctl = WIDGETS[q.widget](q, slot, (resp, selfMark) => submit(q, resp, selfMark, ctl, fb));
    if (S.results[q.id]) showResult(q, S.results[q.id], ctl, fb);   // resumed / revisited
  }

  async function exitQuiz() { await refreshSummary().catch(() => {}); intro(); }

  async function submit(q, response, selfMark, ctl, fb) {
    try {
      const res = await C.api("POST", `/chat/attempts/${S.attemptId}/answer`,
        { question_id: q.id, response, self_mark: selfMark });
      S.results[q.id] = res;
      showResult(q, res, ctl, fb);
    } catch (e) { C.toast(e.message); ctl.reset && ctl.reset(); }
  }

  function showResult(q, res, ctl, fb) {
    ctl.reveal && ctl.reveal(res);
    const last = S.idx === S.play.questions.length - 1;
    const next = btn(last ? "See results" : "Next →", "primary", () => (last ? results() : (S.idx++, question())));
    if (res.pending) {                       // written answer, self-check
      fb.replaceChildren(h("div", { class: "fb part" },
        h("div", { class: "hd" }, "Compare with the model answer"),
        h("div", { class: "ans" }, h("b", {}, "Model answer: "), rich(res.expected)),
        pointsList(res.key_points),
        h("div", { class: "row" }, btn("✓ I got it", "primary", () => submit(q, null, true, ctl, fb)),
          btn("✗ Not quite", "", () => submit(q, null, false, ctl, fb)))));
      return;
    }
    const cls = res.correct ? "ok" : res.score > 0 ? "part" : "no";
    const head = res.correct ? "✓ Correct" : res.score > 0 ? "◐ Partly right" : "✗ Not quite";
    const box = h("div", { class: "fb " + cls }, h("div", { class: "hd" }, head));
    if (res.feedback) box.append(h("div", { class: "exp" }, res.feedback));
    if (!res.correct) box.append(h("div", { class: "ans" }, h("b", {}, "Answer: "), rich(res.expected)));
    if (res.explanation) box.append(h("div", { class: "exp" }, rich(res.explanation)));
    const pts = !res.correct && pointsList(res.key_points);
    if (pts) box.append(pts);
    box.append(h("div", { class: "row" }, next));
    fb.replaceChildren(box);
  }

  function pointsList(points) {
    return points && points.length ? h("ul", {}, points.map((p) => h("li", {}, p))) : null;
  }

  // ── input widgets: (q, slot, submit) → controller {reveal?, reset?} ─────────
  const WIDGETS = {
    choice(q, slot, submit) {
      const opts = q.choices.map((t, i) => {
        const b = h("button", { class: "opt", type: "button" }, h("span", { class: "k" }, String.fromCharCode(65 + i)),
          h("span", {}, rich(t)));
        b.onclick = () => { lock(); b.classList.add("pick"); submit(i); };
        return b;
      });
      const lock = () => opts.forEach((o) => (o.disabled = true));
      slot.append(h("div", { class: "opts" }, opts));
      return { reset: () => opts.forEach((o) => (o.disabled = false)),
        reveal(res) {
          lock();
          opts.forEach((o, i) => {
            if (q.choices[i] === res.expected) o.classList.add("ok");
            else if (o.classList.contains("pick") || (S.results[q.id] && S.results[q.id].response === i)) o.classList.add("no");
          });
        } };
    },
    boolean(q, slot, submit) {
      const mk = (v) => { const b = h("button", { class: "opt", type: "button" }, v); b.dataset.v = v;
        b.onclick = () => { lock(); b.classList.add("pick"); submit(v); }; return b; };
      const opts = [mk("True"), mk("False")];
      const lock = () => opts.forEach((o) => (o.disabled = true));
      slot.append(h("div", { class: "opts" }, opts));
      return { reset: () => opts.forEach((o) => (o.disabled = false)),
        reveal(res) { lock(); opts.forEach((o) => {
          if (o.dataset.v.toLowerCase() === String(res.expected).toLowerCase()) o.classList.add("ok");
          else if (o.classList.contains("pick")) o.classList.add("no"); }); } };
    },
    text(q, slot, submit) {
      const inp = h("input", { type: "text", placeholder: "Type your answer…", autocomplete: "off" });
      const go = btn("Check", "primary", () => { if (inp.value.trim()) { lock(); submit(inp.value.trim()); } });
      const lock = () => { inp.disabled = true; go.disabled = true; };
      inp.addEventListener("keydown", (e) => { if (e.key === "Enter") go.click(); });
      slot.append(inp, h("div", { class: "row" }, go)); setTimeout(() => inp.focus(), 0);
      return { reset: () => { inp.disabled = false; go.disabled = false; }, reveal: lock };
    },
    written(q, slot, submit) {
      const ta = h("textarea", { placeholder: "Write your answer…" });
      const go = btn("Check", "primary", () => { if (ta.value.trim()) { lock(); submit(ta.value.trim()); } });
      const lock = () => { ta.disabled = true; go.disabled = true; };
      slot.append(ta, h("div", { class: "row" }, go));
      return { reset: () => { ta.disabled = false; go.disabled = false; }, reveal: lock };
    },
    match(q, slot, submit) {
      const letters = q.right.map((r) => r.letter);
      const sels = q.left.map(() => h("select", { class: "pick" },
        [h("option", { value: "" }, "—"), ...letters.map((l) => h("option", { value: l }, l))]));
      const go = btn("Check", "primary", () => {
        if (sels.some((s) => !s.value)) return C.toast("Match every item first.");
        lock(); submit(sels.map((s) => s.value));
      });
      const lock = () => { sels.forEach((s) => (s.disabled = true)); go.disabled = true; };
      slot.append(
        ...q.left.map((t, i) => h("div", { class: "mrow" }, h("span", {}, `${i + 1}. ${t}`), sels[i])),
        h("div", { class: "rcol" }, q.right.map((r) => h("div", {}, `${r.letter}. ${r.text}`))),
        h("div", { class: "row" }, go));
      return { reset: () => { sels.forEach((s) => (s.disabled = false)); go.disabled = false; }, reveal: lock };
    },
    order(q, slot, submit) {
      let items = q.items.slice();
      const list = h("div", {});
      const go = btn("Check", "primary", () => { lock(); submit(items.map((i) => i.letter)); });
      let locked = false;
      const lock = () => { locked = true; go.disabled = true; draw(); };
      const move = (i, d) => { const j = i + d; if (j < 0 || j >= items.length) return;
        [items[i], items[j]] = [items[j], items[i]]; draw(); };
      function draw() {
        list.replaceChildren(...items.map((it, i) => h("div", { class: "oitem" },
          h("span", { class: "k" }, `${i + 1}.`), h("span", { class: "tx" }, it.text),
          locked ? null : [h("button", { type: "button", onclick: () => move(i, -1) }, "↑"),
            h("button", { type: "button", onclick: () => move(i, 1) }, "↓")])));
      }
      draw();
      slot.append(h("p", { class: "sub" }, "Put these in the correct order:"), list, h("div", { class: "row" }, go));
      return { reset: () => { locked = false; go.disabled = false; draw(); }, reveal: lock };
    },
  };
  WIDGETS.number = WIDGETS.text;

  // ── results / review ───────────────────────────────────────────────────────
  async function results() {
    try {
      const rv = await C.api("POST", `/chat/attempts/${S.attemptId}/finish`);
      await refreshSummary().catch(() => {});
      showReview(rv, true);
    } catch (e) { C.toast(e.message); }
  }

  async function reviewAttempt(id) {
    try { showReview(await C.api("GET", `/chat/attempts/${id}`), false); }
    catch (e) { C.toast(e.message); }
  }

  function showReview(rv, justFinished) {
    const a = rv.attempt, p = a.percent;
    const msg = p === 100 ? "Perfect score! 🎉" : p >= 80 ? "Great work!" : p >= 50 ? "Good effort — a bit more practice." : "Keep going — review the ones you missed.";
    const nodes = [
      h("h2", {}, justFinished ? "Quiz complete" : "Attempt · " + when(a.created_at)),
      h("div", { class: "score" },
        h("div", { class: "ring", style: `--p:${p}` }, h("b", {}, `${p}%`)),
        h("div", {}, h("div", { style: "font-size:20px;font-weight:700" }, `${fmtScore(a.score)} / ${a.total}`),
          h("div", { class: "muted" }, a.status === "completed" ? msg : "Not finished"))),
    ];
    const types = Object.entries(rv.by_type || {});
    if (types.length > 1) {
      nodes.push(h("div", { class: "sect" }, "By question type"));
      types.forEach(([t, v]) => nodes.push(h("div", { class: "bytype" }, h("span", {}, t.replace(/_/g, " ")),
        h("div", { class: "bar" }, h("i", { style: `width:${pct(v.score, v.of)}%` })), h("span", {}, `${fmtScore(v.score)}/${v.of}`))));
    }
    nodes.push(h("div", { class: "sect" }, "Review"));
    rv.questions.forEach((q) => {
      const ok = q.answered && (q.score || 0) >= 1;
      const det = h("div", { class: "det" });
      if (q.answered) {
        if (q.response != null && q.response !== "") det.append(h("div", { class: "muted" }, "You: " + fmtResponse(q.response)));
        det.append(h("div", {}, h("b", {}, "Answer: "), rich(q.expected)));
        if (q.explanation) det.append(h("div", {}, rich(q.explanation)));
      } else det.append(h("div", { class: "muted" }, "Not answered."));
      nodes.push(h("details", { class: "rv" },
        h("summary", {}, h("span", { class: "mark " + (ok ? "ok" : "no") }, ok ? "✓" : "✗"), h("span", {}, `${q.number}. ${q.prompt.slice(0, 110)}`)), det));
    });
    const row = h("div", { class: "row" });
    if (rv.missed.length) row.append(btn(`Retry the ${rv.missed.length} missed`, "primary", () => retryMissed(a.id)));
    row.append(btn("Play again", rv.missed.length ? "" : "primary", startAttempt), btn("Back", "", async () => { await refreshSummary().catch(() => {}); intro(); }));
    nodes.push(row);
    mount(...nodes);
  }

  const fmtResponse = (r) => (Array.isArray(r) ? r.join(", ") : String(r));

  async function retryMissed(attemptId) {
    try { const s = await C.api("POST", `/chat/attempts/${attemptId}/retry-missed`); C.onNew(s); }
    catch (e) { C.toast(e.message); }
  }
})();
