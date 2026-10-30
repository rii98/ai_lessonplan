"use strict";
/* Owner console — every small operator task without a terminal.

   Structure (so it stays easy to extend):
   - VIEWS is a registry of self-contained sections, each {key, label, render(el)}.
     Adding a section = adding one object; the router below never changes (open/closed).
   - Each view talks only to the public /api/v1 through apiJSON (app.js → lf.js),
     the same contract Edboox's own UI could use.
   - Shared widgets (kpi, table, confirmTyped, statusLine) do one thing each.

   Superadmin grants are shown READ-ONLY by design (rule D5): changing them needs
   the server CLI, so a stolen browser session can never mint new owners. */

// ── shared widgets ───────────────────────────────────────────────────────────
const kpi = (value, label) => h("div", { class: "kpi" },
  h("div", { class: "v" }, value == null ? "—" : String(value)), h("div", { class: "l" }, label));

const okDot = (ok) => h("span", { class: "dot " + (ok ? "ok" : "bad") });

function table(columns, rows, onRow) {
  const t = h("table", { class: "t" },
    h("thead", {}, h("tr", {}, ...columns.map((c) => h("th", {}, c.label)))));
  const body = h("tbody", {});
  if (!rows.length) body.append(h("tr", {}, h("td", { colspan: columns.length, class: "muted" }, "Nothing here yet.")));
  for (const r of rows) {
    const tr = h("tr", { class: onRow ? "click" : "" }, ...columns.map((c) => h("td", {}, c.cell(r))));
    if (onRow) tr.addEventListener("click", () => onRow(r));
    body.append(tr);
  }
  t.append(body);
  return t;
}

function statusLine() {
  const el = h("p", { class: "status" });
  return { el, busy: (m) => setStatus(el, m, "busy"), ok: (m) => setStatus(el, m, ""),
           err: (e) => setStatus(el, e.message || String(e), "err") };
}

/* Irreversible actions: the user types the exact word back. */
function confirmTyped(word, action) {
  const answer = prompt(`${action}\n\nThis cannot be undone. Type ${word} to confirm:`);
  return answer !== null && answer.trim() === word;
}

const when = (iso) => (iso ? fmtDate(iso) : "—");
const json = (o) => h("pre", { class: "json" }, JSON.stringify(o, null, 2));

// ── views ────────────────────────────────────────────────────────────────────
const Overview = {
  key: "overview", label: "📊 Overview",
  async render(el) {
    const d = await apiJSON("GET", "/admin/overview");
    const p = d.people, c = d.content || {};
    el.append(h("h2", {}, "Overview"),
      h("h3", { class: "section" }, "Health"),
      h("div", { class: "kpis" },
        ...Object.entries(d.health).map(([k, ok]) =>
          h("div", { class: "kpi" }, h("div", {}, okDot(ok), h("b", {}, k.replace("_", " "))),
            h("div", { class: "l" }, ok ? "reachable" : "NOT reachable"))),
        h("div", { class: "kpi" }, h("div", {}, okDot(d.schema.current), h("b", {}, "schema")),
          h("div", { class: "l" }, d.schema.current ? `current (${(d.schema.applied || []).length} migrations)`
            : `pending: ${(d.schema.pending || []).join(", ") || d.schema.error}`))),
      h("h3", { class: "section" }, "Edboox sign-in keys"),
      d.edboox.length ? table([
        { label: "Issuer", cell: (r) => h("span", {}, okDot(r.ok), r.name) },
        { label: "Trusted URL", cell: (r) => r.issuer },
        { label: "Keys held (kid)", cell: (r) => (r.kids || []).join(", ") || r.error || "—" },
        { label: "Algorithms", cell: (r) => (r.algorithms || []).join(", ") },
      ], d.edboox) : h("p", { class: "sub" }, "No issuer configured."),
      h("h3", { class: "section" }, "People"),
      h("div", { class: "kpis" }, kpi(p.total, "people seen"), kpi(p["role:student"], "students"),
        kpi(p["role:teacher"], "teachers"), kpi(p.superadmins, "owners (superadmin)"),
        kpi(p.disabled, "disabled")),
      h("h3", { class: "section" }, "Content"),
      h("div", { class: "kpis" },
        kpi((c.documents || {}).lesson, "lessons"), kpi((c.documents || {}).unit, "units"),
        kpi((c.documents || {}).shared, "shared with a school"),
        kpi((c.chat || {}).conversations, "chats"), kpi((c.chat || {}).messages, "chat messages"),
        kpi((c.chat || {}).artifacts, "quizzes made"), kpi((c.chat || {}).attempts, "quiz attempts")),
      h("h3", { class: "section" }, "Knowledge base (all shelves)"),
      h("div", { class: "kpis" }, ...Object.entries(d.corpus).map(([k, v]) => kpi(v, k))),
      h("h3", { class: "section" }, "Settings in effect"), json(d.settings));
  },
};

const People = {
  key: "people", label: "👥 People",
  state: { q: "", role: "", school: "", disabled: "", items: [], cursor: null },
  async render(el) {
    const s = this.state;
    const q = h("input", { placeholder: "name or subject…", value: s.q });
    const role = h("select", {}, ...["", "student", "teacher", "service", "superadmin", "pending"]
      .map((r) => h("option", { value: r }, r || "any role")));
    role.value = s.role;
    const school = h("input", { placeholder: "school id (e.g. sch-7)", value: s.school });
    const dis = h("select", {}, h("option", { value: "" }, "any status"),
      h("option", { value: "false" }, "active"), h("option", { value: "true" }, "disabled"));
    dis.value = s.disabled;
    const list = h("div", {});
    const more = h("button", { class: "small ghost" }, "Load more");
    const load = async (append) => {
      Object.assign(s, { q: q.value.trim(), role: role.value, school: school.value.trim(), disabled: dis.value });
      const params = new URLSearchParams({ limit: "50" });
      for (const k of ["q", "role", "school", "disabled"]) if (s[k]) params.set(k, s[k]);
      if (append && s.cursor) params.set("cursor", s.cursor);
      const page = await apiJSON("GET", `/admin/people?${params}`);
      s.items = append ? s.items.concat(page.items) : page.items;
      s.cursor = page.next_cursor;
      more.style.display = s.cursor ? "" : "none";
      list.textContent = "";
      list.append(table([
        { label: "Name", cell: (r) => r.name || "—" },
        { label: "Edboox id", cell: (r) => r.subject },
        { label: "Role", cell: (r) => r.superadmin ? h("span", { class: "pill ok" }, "owner") : r.role },
        { label: "School", cell: (r) => r.school || "—" },
        { label: "Last seen", cell: (r) => when(r.last_seen_at) },
        { label: "Status", cell: (r) => r.disabled ? h("span", { class: "pill warn" }, "disabled") : "active" },
      ], s.items, (r) => Router.go(`person/${r.id}`)));
    };
    for (const c of [q, school]) c.addEventListener("keydown", (e) => { if (e.key === "Enter") load(false); });
    for (const c of [role, dis]) c.addEventListener("change", () => load(false));
    more.addEventListener("click", () => load(true));
    el.append(h("h2", {}, "People"),
      h("p", { class: "sub" }, "Everyone who has opened LessonForge from Edboox. To add or remove a student or teacher, do it in Edboox — LessonForge only sees people after they sign in."),
      h("div", { class: "filters" }, q, role, school, dis,
        h("button", { class: "small", onclick: () => load(false) }, "Search")),
      list, more);
    await load(false);
  },
};

const Person = {
  key: "person", label: null,   // reached from People; not in the side nav
  async render(el, id) {
    const st = statusLine();
    const draw = async () => {
      const r = await apiJSON("GET", `/admin/people/${id}`);
      el.textContent = "";
      const ctx = r.context || {};
      el.append(
        h("p", {}, h("a", { href: "#people" }, "← People")),
        h("h2", {}, r.name || r.subject, " ",
          r.superadmin ? h("span", { class: "pill ok" }, "owner") : null,
          r.disabled ? h("span", { class: "pill warn" }, "disabled") : null),
        h("div", { class: "kpis" }, kpi(r.role, "role"), kpi(r.school, "school (signed)"),
          kpi(r.subject, "Edboox id"), kpi(when(r.last_seen_at), "last seen"),
          kpi(when(r.created_at), "first seen")),
        h("h3", { class: "section" }, "What Edboox told us"),
        h("p", { class: "sub" }, r.context_synced_at ? `Last refreshed ${when(r.context_synced_at)} (source: ${ctx.source || "?"}). Used only to personalise — never to decide access.`
          : "Not fetched yet."),
        r.context ? json(ctx) : null,
        h("h3", { class: "section" }, "Actions"),
        r.superadmin
          ? h("p", { class: "sub" }, "Owners can't be disabled or deleted here. Revoke first on the server: ",
              h("code", { class: "cmd" }, `lessonforge-admin revoke --subject ${r.subject}`))
          : null,
        h("div", { class: "toolbar" },
          h("button", { class: "small", onclick: () => act("sync-context", "Asking Edboox…") }, "↻ Refresh from Edboox"),
          r.superadmin ? null : r.disabled
            ? h("button", { class: "small primary", onclick: () => act("enable", "Enabling…") }, "Enable")
            : h("button", { class: "small", onclick: () => act("disable", "Disabling…") }, "⏸ Disable"),
          r.superadmin ? null : h("button", { class: "small danger", onclick: () => forget(r) }, "🗑 Delete their data")),
        st.el,
        h("p", { class: "sub" }, "Disable is a local kill switch (takes effect within a minute). Delete removes their lessons, chats and profile; their usage stays in reports anonymously."));
    };
    const act = async (verb, msg) => {
      st.busy(msg);
      try { await apiJSON("POST", `/admin/people/${id}/${verb}`); await draw(); st.ok("Done."); }
      catch (e) { st.err(e); }
    };
    const forget = async (r) => {
      if (!confirmTyped(r.subject, `Delete everything LessonForge holds about ${r.name || r.subject}?`)) return;
      st.busy("Deleting…");
      try { await apiJSON("POST", `/admin/people/${id}/forget`, { confirm: r.subject }); Router.go("people"); }
      catch (e) { st.err(e); }
    };
    await draw();
  },
};

const Owners = {
  key: "owners", label: "🔑 Owners",
  async render(el) {
    const d = await apiJSON("GET", "/admin/grants");
    el.append(h("h2", {}, "Owners (superadmin)"),
      h("p", { class: "sub" }, "Owner rights are granted only on the server, never from a browser — so a stolen session or a compromised Edboox can't create new owners. Every change is in the audit log."),
      table([
        { label: "Edboox id", cell: (g) => `${g.issuer}:${g.subject}` },
        { label: "Role", cell: (g) => g.role },
        { label: "Granted by", cell: (g) => g.granted_by },
        { label: "When", cell: (g) => when(g.granted_at) },
      ], d.items),
      h("h3", { class: "section" }, "To change"),
      h("p", {}, h("code", { class: "cmd" }, "docker compose exec api lessonforge-admin grant --subject teacher:7")),
      h("p", {}, h("code", { class: "cmd" }, "docker compose exec api lessonforge-admin revoke --subject teacher:7")));
  },
};

const Knowledge = {
  key: "knowledge", label: "📚 Knowledge",
  async render(el) {
    const st = statusLine();
    const spaces = (await apiJSON("GET", "/corpus/spaces")).spaces;
    const space = h("select", {}, h("option", { value: "" }, "all shelves"),
      ...spaces.map((s) => h("option", { value: s.id }, `${s.name} (${s.id})`)));
    const coll = h("select", {}, h("option", { value: "" }, "all collections"),
      ...["reference", "curriculum", "pedagogical", "exemplar", "local_context"].map((c) => h("option", { value: c }, c)));
    const list = h("div", {});
    const load = async () => {
      const params = new URLSearchParams({ limit: "200" });
      if (space.value) params.set("space", space.value);
      if (coll.value) params.set("collection", coll.value);
      const d = await apiJSON("GET", `/corpus/sources?${params}`);
      list.textContent = "";
      list.append(table([
        { label: "Title", cell: (s) => s.title },
        { label: "Shelf", cell: (s) => s.space },
        { label: "Collection", cell: (s) => s.collection },
        { label: "Pieces", cell: (s) => s.chunks },
        { label: "Added", cell: (s) => when(s.ingested_at) },
        { label: "", cell: (s) => h("button", { class: "small danger", onclick: (e) => { e.stopPropagation(); remove(s); } }, "Remove") },
      ], d.items));
    };
    const remove = async (s) => {
      if (!confirmTyped("remove", `Remove "${s.title}" (${s.chunks} pieces) from the ${s.space} shelf?`)) return;
      st.busy("Removing…");
      try {
        await apiJSON("DELETE", `/corpus/sources/${s.collection}/${encodeURIComponent(s.doc_id)}?space=${encodeURIComponent(s.space)}`);
        st.ok("Removed."); await load();
      } catch (e) { st.err(e); }
    };
    space.addEventListener("change", load);
    coll.addEventListener("change", load);
    el.append(h("div", { class: "section-h" }, h("h2", {}, "Knowledge base"),
        h("a", { class: "small", href: "/corpus" }, "➕ Add books & records")),
      h("p", { class: "sub" }, "The record book: every document ingested, on which shelf. Removing one deletes all of its pieces from search."),
      h("div", { class: "filters" }, space, coll), st.el, list);
    await load();
  },
};

const Usage = {
  key: "usage", label: "📈 Usage",
  async render(el) {
    const today = new Date();
    const from = h("input", { type: "date", value: new Date(today - 29 * 864e5).toISOString().slice(0, 10) });
    const to = h("input", { type: "date", value: today.toISOString().slice(0, 10) });
    const group = h("select", {}, ...["day", "school", "role", "school,role", "day,school,role"]
      .map((g) => h("option", { value: g }, "by " + g.replace(/,/g, " + "))));
    const out = h("div", {});
    const load = async () => {
      const range = `from=${from.value}T00:00:00Z&to=${to.value}T23:59:59Z`;
      const [u, t, q] = await Promise.all([
        apiJSON("GET", `/reports/usage?${range}&group_by=${group.value}`),
        apiJSON("GET", `/reports/chat-topics?${range}`),
        apiJSON("GET", `/reports/quizzes?${range}`)]);
      const keys = group.value.split(",");
      out.textContent = "";
      out.append(table([
        ...keys.map((k) => ({ label: k, cell: (r) => String(r[k] ?? "—") })),
        { label: "active users", cell: (r) => r.active_users }, { label: "chat msgs", cell: (r) => r.chat_messages },
        { label: "quizzes", cell: (r) => r.quizzes_created }, { label: "attempts", cell: (r) => r.quiz_attempts },
        { label: "lessons", cell: (r) => r.lessons }, { label: "units", cell: (r) => r.units },
        { label: "AI tokens in/out", cell: (r) => `${r.tokens_in} / ${r.tokens_out}` },
      ], u.rows),
      h("h3", { class: "section" }, "What students ask about"),
      h("p", { class: "sub" }, `${t.turns} questions; ${t.grounded_share == null ? "—" : Math.round(t.grounded_share * 100) + "%"} answered from the books.`),
      table([{ label: "Chapter", cell: (r) => r.chapter }, { label: "Subject", cell: (r) => r.subject || "—" },
             { label: "Questions", cell: (r) => r.turns }], t.chapters),
      h("h3", { class: "section" }, "Quizzes"),
      h("div", { class: "kpis" }, kpi(q.created, "made"), kpi(q.attempts_finished, "finished"),
        kpi(q.completion_rate == null ? null : Math.round(q.completion_rate * 100) + "%", "completion"),
        kpi(q.avg_percent == null ? null : q.avg_percent + "%", "average score")),
      q.weakest_chapters.length ? table([{ label: "Weakest chapter", cell: (r) => r.chapter },
        { label: "Avg %", cell: (r) => r.avg_percent }, { label: "Attempts", cell: (r) => r.attempts }], q.weakest_chapters) : null);
    };
    for (const c of [from, to, group]) c.addEventListener("change", load);
    el.append(h("h2", {}, "Usage"),
      h("p", { class: "sub" }, "The same content-free reports Edboox pulls. No student's words are ever stored."),
      h("div", { class: "filters" }, from, to, group), out);
    await load();
  },
};

const Audit = {
  key: "audit", label: "🧾 Audit log",
  async render(el) {
    const filter = h("select", {}, ...[["", "all actions"], ["grant.", "owner grants"], ["principal.", "people"],
      ["corpus.", "knowledge base"], ["maintenance.", "maintenance"]].map(([v, l]) => h("option", { value: v }, l)));
    const list = h("div", {});
    const more = h("button", { class: "small ghost" }, "Older");
    let items = [], cursor = null;
    const load = async (append) => {
      const params = new URLSearchParams({ limit: "50" });
      if (filter.value) params.set("action", filter.value);
      if (append && cursor) params.set("before", cursor);
      const d = await apiJSON("GET", `/admin/audit?${params}`);
      items = append ? items.concat(d.items) : d.items;
      cursor = d.next_cursor;
      more.style.display = cursor ? "" : "none";
      list.textContent = "";
      list.append(table([
        { label: "When", cell: (e) => when(e.at) }, { label: "Who", cell: (e) => e.actor },
        { label: "Action", cell: (e) => e.action }, { label: "Target", cell: (e) => e.target || "—" },
        { label: "Detail", cell: (e) => Object.keys(e.detail || {}).length ? JSON.stringify(e.detail).slice(0, 140) : "" },
      ], items));
    };
    filter.addEventListener("change", () => load(false));
    more.addEventListener("click", () => load(true));
    el.append(h("h2", {}, "Audit log"),
      h("p", { class: "sub" }, "Security-relevant actions only: owner grants, people changes, knowledge-base changes, maintenance."),
      h("div", { class: "filters" }, filter), list, more);
    await load(false);
  },
};

const Maintenance = {
  key: "maintenance", label: "🧹 Maintenance",
  async render(el) {
    const d = await apiJSON("GET", "/admin/overview");
    const purge = statusLine(), backfill = statusLine();
    el.append(h("h2", {}, "Maintenance"),
      h("div", { class: "card", style: "padding:14px" },
        h("h3", {}, "Clean up"),
        h("p", { class: "sub" }, `Deletes used sign-in passes that have expired, and usage events older than ${d.settings.retention_days} days. Safe to run any time (also good as a nightly job).`),
        h("button", { class: "small primary", onclick: async () => {
          purge.busy("Cleaning…");
          try { const r = await apiJSON("POST", "/admin/maintenance/purge");
                purge.ok(`Removed ${r.launch_tokens} expired pass(es) and ${r.activity_events} old event(s).`); }
          catch (e) { purge.err(e); }
        } }, "Run clean-up"), purge.el),
      h("div", { class: "card", style: "padding:14px" },
        h("h3", {}, "Put old books on the shared shelf"),
        h("p", { class: "sub" }, "Only needed once, after upgrading a knowledge base that was filled before shelves existed: those pieces are invisible until they have a shelf. Running it again does nothing."),
        h("button", { class: "small", onclick: async () => {
          backfill.busy("Tagging…");
          try { const r = await apiJSON("POST", "/admin/maintenance/backfill-spaces");
                backfill.ok("Tagged: " + Object.entries(r).map(([k, v]) => `${k} ${v}`).join(", ")); }
          catch (e) { backfill.err(e); }
        } }, "Backfill shelves"), backfill.el),
      h("div", { class: "card", style: "padding:14px" },
        h("h3", {}, "Database schema"),
        h("p", {}, okDot(d.schema.current), d.schema.current ? "Up to date." : "Behind the code — run the migration step on the server."),
        json(d.schema)));
  },
};

// ── router ───────────────────────────────────────────────────────────────────
const VIEWS = [Overview, People, Person, Owners, Knowledge, Usage, Audit, Maintenance];

const Router = {
  go(hash) { location.hash = hash; },
  async render() {
    const [key, arg] = (location.hash.slice(1) || "overview").split("/");
    const view = VIEWS.find((v) => v.key === key) || Overview;
    const active = view === Person ? "people" : view.key;
    const side = $("side");
    side.textContent = "";
    for (const v of VIEWS.filter((x) => x.label)) {
      side.append(h("a", { href: "#" + v.key, class: v.key === active ? "active" : "" }, v.label));
    }
    const el = $("view");
    el.textContent = "";
    el.append(h("p", { class: "sub" }, h("span", { class: "spinner" }), " Loading…"));
    try {
      const host = h("div", {});
      await view.render(host, arg);
      el.textContent = "";
      el.append(host);
    } catch (e) {
      el.textContent = "";
      el.append(h("p", { class: "status err" }, e.message));
    }
  },
};

mountNav("admin", "🛡️ Owner console", "Run LessonForge without a terminal — people, knowledge, usage, audit and upkeep.");
window.addEventListener("hashchange", () => Router.render());
lfGuard().then(() => Router.render());
