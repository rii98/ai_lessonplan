import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { describeError, useApi } from "@/lib/api";
import { dateTime, humanize } from "@/lib/format";
import { useDebouncedValue } from "@/lib/hooks";
import { Badge, Banner, Button, ConfirmDialog, ErrorState, Icon, Muted, Section, Select, SkeletonLines, Stack, TextInput, useToast } from "@/ui";
import { Cmd, Json, Kpi, Kpis, Table } from "./parts";
import s from "./admin.module.css";

// Contract gap: every /admin and /reports route returns an untyped dict. The
// shapes used below are read loosely (`any` at this one boundary), never stored.
/* eslint-disable @typescript-eslint/no-explicit-any */
type Loose = any;

function useGet(path: string, query?: Record<string, string | number | null | undefined>) {
  const api = useApi();
  return useQuery({ queryKey: ["admin", path, query], queryFn: ({ signal }) => api.get<Loose>(path, query, signal) });
}

const Loading = () => <SkeletonLines lines={6} />;
const Failed = ({ q }: { q: { error: unknown; refetch: () => void } }) => <ErrorState error={describeError(q.error)} onRetry={() => q.refetch()} />;

export function Overview() {
  const q = useGet("/admin/overview");
  if (q.isPending) return <Loading />;
  if (q.error) return <Failed q={q} />;
  const d = q.data, p = d.people ?? {}, c = d.content ?? {};
  return (
    <Stack gap={8}>
      <Section title="Health">
        <Kpis>
          {Object.entries(d.health ?? {}).map(([k, ok]) => <Kpi key={k} value={ok ? "OK" : "Down"} label={humanize(k)} ok={!!ok} />)}
          <Kpi value={d.schema?.current ? "Current" : "Behind"} label={`schema · ${(d.schema?.applied ?? []).length} migrations`} ok={!!d.schema?.current} />
        </Kpis>
      </Section>
      <Section title="Edboox sign-in keys">
        <Table rowKey={(r: Loose) => r.issuer} rows={d.edboox ?? []} empty="No issuer configured." columns={[
          { label: "Issuer", cell: (r: Loose) => <span style={{ display: "flex", gap: 6, alignItems: "center" }}><Badge tone={r.ok ? "pine" : "brick"}>{r.ok ? "ok" : "error"}</Badge>{r.name}</span> },
          { label: "Trusted URL", cell: (r: Loose) => r.issuer },
          { label: "Keys (kid)", cell: (r: Loose) => (r.kids ?? []).join(", ") || r.error || "—" },
          { label: "Algorithms", cell: (r: Loose) => (r.algorithms ?? []).join(", ") },
        ]} />
      </Section>
      <Section title="People">
        <Kpis>
          <Kpi value={p.total} label="people seen" /><Kpi value={p["role:student"]} label="students" /><Kpi value={p["role:teacher"]} label="teachers" />
          <Kpi value={p.superadmins} label="owners" /><Kpi value={p.disabled} label="disabled" />
        </Kpis>
      </Section>
      <Section title="Content">
        <Kpis>
          <Kpi value={c.documents?.lesson} label="lessons" /><Kpi value={c.documents?.unit} label="units" /><Kpi value={c.documents?.shared} label="shared" />
          <Kpi value={c.chat?.conversations} label="chats" /><Kpi value={c.chat?.messages} label="messages" /><Kpi value={c.chat?.artifacts} label="quizzes" /><Kpi value={c.chat?.attempts} label="attempts" />
        </Kpis>
      </Section>
      <Section title="Knowledge base (all shelves)">
        <Kpis>{Object.entries(d.corpus ?? {}).map(([k, v]) => <Kpi key={k} value={v as number} label={humanize(k)} />)}</Kpis>
      </Section>
      <Section title="Settings in effect"><Json value={d.settings} /></Section>
    </Stack>
  );
}

export function People() {
  const api = useApi();
  const navigate = useNavigate();
  const [f, setF] = useState({ q: "", role: "", school: "", disabled: "" });
  const dq = useDebouncedValue(f, 300);
  const list = useInfiniteQuery({
    queryKey: ["admin", "people", dq],
    initialPageParam: null as string | null,
    queryFn: ({ pageParam, signal }) => api.get<Loose>("/admin/people", { limit: 50, ...dq, cursor: pageParam }, signal),
    getNextPageParam: (p: Loose) => p.next_cursor ?? null,
  });
  const rows = list.data?.pages.flatMap((p: Loose) => p.items) ?? [];
  return (
    <Stack gap={12}>
      <Muted>Everyone who has opened LessonForge from Edboox. To add or remove a person, do it in Edboox — LessonForge only sees people after they sign in.</Muted>
      <div className={s.filters}>
        <TextInput placeholder="Name or Edboox id" aria-label="Search people" value={f.q} onChange={(e) => setF({ ...f, q: e.target.value })} />
        <Select aria-label="Role" value={f.role} onChange={(e) => setF({ ...f, role: e.target.value })}>
          {["", "student", "teacher", "service", "superadmin", "pending"].map((r) => <option key={r} value={r}>{r || "Any role"}</option>)}
        </Select>
        <TextInput placeholder="School id" aria-label="School" value={f.school} onChange={(e) => setF({ ...f, school: e.target.value })} />
        <Select aria-label="Status" value={f.disabled} onChange={(e) => setF({ ...f, disabled: e.target.value })}>
          <option value="">Any status</option><option value="false">Active</option><option value="true">Disabled</option>
        </Select>
      </div>
      {list.isPending ? <Loading /> : list.error ? <Failed q={list} /> : (
        <Table rowKey={(r: Loose) => r.id} rows={rows} onRow={(r: Loose) => navigate(`/admin/people/${r.id}`)} columns={[
          { label: "Name", cell: (r: Loose) => r.name || "—" },
          { label: "Edboox id", cell: (r: Loose) => r.subject },
          { label: "Role", cell: (r: Loose) => (r.superadmin ? <Badge tone="pine">owner</Badge> : r.role) },
          { label: "School", cell: (r: Loose) => r.school || "—" },
          { label: "Last seen", cell: (r: Loose) => dateTime(r.last_seen_at) },
          { label: "Status", cell: (r: Loose) => (r.disabled ? <Badge tone="brick">disabled</Badge> : "active") },
        ]} />
      )}
      {list.hasNextPage && <div><Button busy={list.isFetchingNextPage} onClick={() => list.fetchNextPage()}>Load more</Button></div>}
    </Stack>
  );
}

export function Person() {
  const { id = "" } = useParams();
  const api = useApi();
  const qc = useQueryClient();
  const navigate = useNavigate();
  const toast = useToast();
  const q = useGet(`/admin/people/${encodeURIComponent(id)}`);
  const [forget, setForget] = useState(false);
  const act = useMutation({
    mutationFn: ({ verb, body }: { verb: string; body?: unknown }) => api.post(`/admin/people/${encodeURIComponent(id)}/${verb}`, body),
    onSuccess: (_, v) => { if (v.verb === "forget") navigate("/admin/people"); else { toast.show("Done"); void qc.invalidateQueries({ queryKey: ["admin"] }); } },
    onError: (e) => toast.error(describeError(e)),
  });
  if (q.isPending) return <Loading />;
  if (q.error) return <Failed q={q} />;
  const r = q.data;
  return (
    <Stack gap={8}>
      <Link to="/admin/people" style={{ fontSize: "var(--text-sm)" }}>← People</Link>
      <h2 style={{ display: "flex", gap: 8, alignItems: "center" }}>{r.name || r.subject}{r.superadmin && <Badge tone="pine">owner</Badge>}{r.disabled && <Badge tone="brick">disabled</Badge>}</h2>
      <Kpis><Kpi value={r.role} label="role" /><Kpi value={r.school || "—"} label="school (signed)" /><Kpi value={dateTime(r.last_seen_at)} label="last seen" /><Kpi value={dateTime(r.created_at)} label="first seen" /></Kpis>
      <Section title="What Edboox told us" hint={r.context_synced_at ? `Refreshed ${dateTime(r.context_synced_at)}. Used to personalise, never to decide access.` : "Not fetched yet."}>
        {r.context ? <Json value={r.context} /> : <Muted>No context yet.</Muted>}
      </Section>
      <Section title="Actions">
        {r.superadmin && <Banner tone="info">Owners can't be disabled or deleted here. Revoke on the server first: <Cmd>{`lessonforge-admin revoke --subject ${r.subject}`}</Cmd></Banner>}
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginTop: 8 }}>
          <Button icon={<Icon.RefreshCw size={15} />} busy={act.isPending && act.variables?.verb === "sync-context"} onClick={() => act.mutate({ verb: "sync-context" })}>Refresh from Edboox</Button>
          {!r.superadmin && (r.disabled
            ? <Button variant="primary" busy={act.isPending && act.variables?.verb === "enable"} onClick={() => act.mutate({ verb: "enable" })}>Enable</Button>
            : <Button busy={act.isPending && act.variables?.verb === "disable"} onClick={() => act.mutate({ verb: "disable" })}>Disable</Button>)}
          {!r.superadmin && <Button variant="danger" icon={<Icon.Trash2 size={15} />} onClick={() => setForget(true)}>Delete their data</Button>}
        </div>
        <p style={{ marginTop: 12, fontSize: "var(--text-xs)", color: "var(--ink-3)" }}>Disable is a local kill switch (takes effect within a minute). Delete removes their lessons, chats and profile; usage stays in reports, anonymously.</p>
      </Section>
      <ConfirmDialog open={forget} onOpenChange={setForget} danger typeToConfirm={r.subject} busy={act.isPending}
        title={`Delete everything about ${r.name || r.subject}?`} confirmLabel="Delete their data" body="Their lessons, units, chats and profile are permanently removed."
        onConfirm={() => act.mutate({ verb: "forget", body: { confirm: r.subject } })} />
    </Stack>
  );
}

export function Owners() {
  const q = useGet("/admin/grants");
  if (q.isPending) return <Loading />;
  if (q.error) return <Failed q={q} />;
  return (
    <Stack gap={12}>
      <Muted>Owner rights are granted only on the server, never from a browser — so a stolen session can't create new owners. Every change is in the audit log.</Muted>
      <Table rowKey={(g: Loose) => `${g.issuer}:${g.subject}`} rows={q.data.items ?? []} columns={[
        { label: "Edboox id", cell: (g: Loose) => `${g.issuer}:${g.subject}` }, { label: "Role", cell: (g: Loose) => g.role },
        { label: "Granted by", cell: (g: Loose) => g.granted_by }, { label: "When", cell: (g: Loose) => dateTime(g.granted_at) },
      ]} />
      <Section title="To change">
        <Stack gap={6}><Cmd>docker compose exec api lessonforge-admin grant --subject teacher:7</Cmd><Cmd>docker compose exec api lessonforge-admin revoke --subject teacher:7</Cmd></Stack>
      </Section>
    </Stack>
  );
}

export function Sources() {
  const api = useApi();
  const qc = useQueryClient();
  const toast = useToast();
  const spaces = useGet("/corpus/spaces");
  const [space, setSpace] = useState("");
  const [coll, setColl] = useState("");
  const q = useGet("/corpus/sources", { limit: 200, space: space || null, collection: coll || null });
  const [removing, setRemoving] = useState<Loose>(null);
  const del = useMutation({
    mutationFn: (x: Loose) => api.request("DELETE", `/corpus/sources/${x.collection}/${encodeURIComponent(x.doc_id)}`, { query: { space: x.space } }),
    onSuccess: () => { toast.show("Removed"); setRemoving(null); void qc.invalidateQueries({ queryKey: ["admin"] }); },
    onError: (e) => toast.error(describeError(e)),
  });
  return (
    <Stack gap={12}>
      <Muted>Every document ingested, and on which shelf. Removing one deletes all its pieces from search. <Link to="/corpus">Add books & records →</Link></Muted>
      <div className={s.filters}>
        <Select aria-label="Shelf" value={space} onChange={(e) => setSpace(e.target.value)}>
          <option value="">All shelves</option>{(spaces.data?.spaces ?? []).map((x: Loose) => <option key={x.id} value={x.id}>{x.name}</option>)}
        </Select>
        <Select aria-label="Collection" value={coll} onChange={(e) => setColl(e.target.value)}>
          <option value="">All collections</option>{["reference", "curriculum", "pedagogical", "exemplar", "local_context"].map((c) => <option key={c} value={c}>{humanize(c)}</option>)}
        </Select>
      </div>
      {q.isPending ? <Loading /> : q.error ? <Failed q={q} /> : (
        <Table rowKey={(x: Loose) => `${x.space}:${x.collection}:${x.doc_id}`} rows={q.data.items ?? []} columns={[
          { label: "Title", cell: (x: Loose) => x.title }, { label: "Shelf", cell: (x: Loose) => x.space }, { label: "Collection", cell: (x: Loose) => humanize(x.collection) },
          { label: "Pieces", cell: (x: Loose) => x.chunks }, { label: "Added", cell: (x: Loose) => dateTime(x.ingested_at) },
          { label: "", cell: (x: Loose) => <Button size="sm" variant="danger" onClick={() => setRemoving(x)}>Remove</Button> },
        ]} />
      )}
      <ConfirmDialog open={!!removing} onOpenChange={(o) => !o && setRemoving(null)} danger typeToConfirm="remove" busy={del.isPending}
        title={`Remove “${removing?.title}”?`} body={`All ${removing?.chunks} pieces leave the ${removing?.space} shelf.`} confirmLabel="Remove" onConfirm={() => del.mutate(removing)} />
    </Stack>
  );
}

const iso = (d: Date) => d.toISOString().slice(0, 10);

export function Usage() {
  const [from, setFrom] = useState(iso(new Date(Date.now() - 29 * 864e5)));
  const [to, setTo] = useState(iso(new Date()));
  const [group, setGroup] = useState("day");
  const range = { from: `${from}T00:00:00Z`, to: `${to}T23:59:59Z` };
  const u = useGet("/reports/usage", { ...range, group_by: group });
  const t = useGet("/reports/chat-topics", range);
  const qz = useGet("/reports/quizzes", range);
  const keys = group.split(",");
  return (
    <Stack gap={12}>
      <Muted>The same content-free reports Edboox pulls. No student's words are stored.</Muted>
      <div className={s.filters}>
        <TextInput type="date" aria-label="From" value={from} onChange={(e) => setFrom(e.target.value)} />
        <TextInput type="date" aria-label="To" value={to} onChange={(e) => setTo(e.target.value)} />
        <Select aria-label="Group by" value={group} onChange={(e) => setGroup(e.target.value)}>
          {["day", "school", "role", "school,role", "day,school,role"].map((g) => <option key={g} value={g}>by {g.replace(/,/g, " + ")}</option>)}
        </Select>
      </div>
      {u.isPending ? <Loading /> : u.error ? <Failed q={u} /> : (
        <Table rowKey={(_r, i) => String(i)} rows={u.data.rows ?? []} columns={[
          ...keys.map((k) => ({ label: k, cell: (r: Loose) => String(r[k] ?? "—") })),
          { label: "Active users", cell: (r: Loose) => r.active_users }, { label: "Chat msgs", cell: (r: Loose) => r.chat_messages },
          { label: "Quizzes", cell: (r: Loose) => r.quizzes_created }, { label: "Attempts", cell: (r: Loose) => r.quiz_attempts },
          { label: "Lessons", cell: (r: Loose) => r.lessons }, { label: "Units", cell: (r: Loose) => r.units },
          { label: "AI tokens in/out", cell: (r: Loose) => `${r.tokens_in} / ${r.tokens_out}` },
        ]} />
      )}
      <Section title="What students ask about" hint={t.data ? `${t.data.turns} questions; ${t.data.grounded_share == null ? "—" : `${Math.round(t.data.grounded_share * 100)}%`} answered from the books.` : undefined}>
        {t.isPending ? <Loading /> : t.error ? <Failed q={t} /> : (
          <Table rowKey={(r: Loose) => `${r.chapter}:${r.subject}`} rows={t.data.chapters ?? []} columns={[
            { label: "Chapter", cell: (r: Loose) => r.chapter }, { label: "Subject", cell: (r: Loose) => r.subject || "—" }, { label: "Questions", cell: (r: Loose) => r.turns },
          ]} />
        )}
      </Section>
      <Section title="Quizzes">
        {qz.isPending ? <Loading /> : qz.error ? <Failed q={qz} /> : (<>
          <Kpis>
            <Kpi value={qz.data.created} label="made" /><Kpi value={qz.data.attempts_finished} label="finished" />
            <Kpi value={qz.data.completion_rate == null ? null : `${Math.round(qz.data.completion_rate * 100)}%`} label="completion" />
            <Kpi value={qz.data.avg_percent == null ? null : `${qz.data.avg_percent}%`} label="average score" />
          </Kpis>
          {!!qz.data.weakest_chapters?.length && <div style={{ marginTop: 12 }}><Table rowKey={(r: Loose) => r.chapter} rows={qz.data.weakest_chapters} columns={[
            { label: "Weakest chapter", cell: (r: Loose) => r.chapter }, { label: "Avg %", cell: (r: Loose) => r.avg_percent }, { label: "Attempts", cell: (r: Loose) => r.attempts },
          ]} /></div>}
        </>)}
      </Section>
    </Stack>
  );
}

export function Audit() {
  const api = useApi();
  const [action, setAction] = useState("");
  const list = useInfiniteQuery({
    queryKey: ["admin", "audit", action],
    initialPageParam: null as string | null,
    queryFn: ({ pageParam, signal }) => api.get<Loose>("/admin/audit", { limit: 50, action: action || null, before: pageParam }, signal),
    getNextPageParam: (p: Loose) => p.next_cursor ?? null,
  });
  const rows = list.data?.pages.flatMap((p: Loose) => p.items) ?? [];
  return (
    <Stack gap={12}>
      <Muted>Security-relevant actions only: owner grants, people changes, knowledge-base changes, maintenance.</Muted>
      <div className={s.filters}>
        <Select aria-label="Action" value={action} onChange={(e) => setAction(e.target.value)}>
          {[["", "All actions"], ["grant.", "Owner grants"], ["principal.", "People"], ["corpus.", "Knowledge base"], ["maintenance.", "Maintenance"]].map(([v, l]) => <option key={v} value={v}>{l}</option>)}
        </Select>
      </div>
      {list.isPending ? <Loading /> : list.error ? <Failed q={list} /> : (
        <Table rowKey={(e: Loose, i) => `${e.at}:${i}`} rows={rows} columns={[
          { label: "When", cell: (e: Loose) => dateTime(e.at) }, { label: "Who", cell: (e: Loose) => e.actor }, { label: "Action", cell: (e: Loose) => e.action },
          { label: "Target", cell: (e: Loose) => e.target || "—" },
          { label: "Detail", cell: (e: Loose) => (Object.keys(e.detail ?? {}).length ? <code style={{ fontSize: 11 }}>{JSON.stringify(e.detail).slice(0, 140)}</code> : "") },
        ]} />
      )}
      {list.hasNextPage && <div><Button busy={list.isFetchingNextPage} onClick={() => list.fetchNextPage()}>Older</Button></div>}
    </Stack>
  );
}

export function Maintenance() {
  const api = useApi();
  const q = useGet("/admin/overview");
  const purge = useMutation({ mutationFn: () => api.post<Record<string, number>>("/admin/maintenance/purge") });
  const backfill = useMutation({ mutationFn: () => api.post<Record<string, number>>("/admin/maintenance/backfill-spaces") });
  if (q.isPending) return <Loading />;
  if (q.error) return <Failed q={q} />;
  const d = q.data;
  return (
    <Stack gap={8}>
      <Section title="Clean up" hint={`Deletes expired sign-in passes and usage events older than ${d.settings?.retention_days ?? "the retention"} days. Safe any time.`}>
        <Button variant="primary" busy={purge.isPending} onClick={() => purge.mutate()}>Run clean-up</Button>
        {purge.data && <div style={{ marginTop: 10 }}><Banner tone="success">Removed {purge.data.launch_tokens} expired pass(es) and {purge.data.activity_events} old event(s).</Banner></div>}
        {purge.error && <div style={{ marginTop: 10 }}><Banner tone="danger">{describeError(purge.error)}</Banner></div>}
      </Section>
      <Section title="Put old books on the shared shelf" hint="Only needed once, after upgrading a knowledge base filled before shelves existed. Running it again does nothing.">
        <Button busy={backfill.isPending} onClick={() => backfill.mutate()}>Backfill shelves</Button>
        {backfill.data && <div style={{ marginTop: 10 }}><Banner tone="success">Tagged: {Object.entries(backfill.data).map(([k, v]) => `${k} ${v}`).join(", ")}</Banner></div>}
        {backfill.error && <div style={{ marginTop: 10 }}><Banner tone="danger">{describeError(backfill.error)}</Banner></div>}
      </Section>
      <Section title="Database schema">
        <Banner tone={d.schema?.current ? "success" : "warning"}>{d.schema?.current ? "Up to date." : "Behind the code — run the migration step on the server."}</Banner>
        <div style={{ marginTop: 10 }}><Json value={d.schema} /></div>
      </Section>
    </Stack>
  );
}
