import { useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useMe } from "@/features/session";
import { describeError, type DocumentKind, type StoredDocument } from "@/lib/api";
import { relativeTime } from "@/lib/format";
import { useDebouncedValue, useStoredState } from "@/lib/hooks";
import {
  Badge, Button, ConfirmDialog, EmptyState, ErrorState, Icon, IconButton, Menu, Page, PageHeader, Segmented, Skeleton, TextInput, useToast,
} from "@/ui";
import { useDocumentActions, useDocuments, type Scope } from "./api";
import s from "./library.module.css";

const href = (d: StoredDocument) => (d.kind === "unit" ? `/units/${d.id}` : `/lessons/${d.id}`);

/** `/library` — everything you've made, and what colleagues share with your school. */
export function LibraryPage() {
  const navigate = useNavigate();
  const toast = useToast();
  const me = useMe().data;
  const [scope, setScope] = useStoredState<Scope>("lf.library.scope", "mine");
  const [kind, setKind] = useStoredState<"all" | DocumentKind>("lf.library.kind", "all");
  const [search, setSearch] = useState("");
  const q = useDebouncedValue(search.trim(), 300);
  const list = useDocuments(scope, kind === "all" ? null : kind, q);
  const act = useDocumentActions();
  const [deleting, setDeleting] = useState<StoredDocument | null>(null);

  const items = useMemo(() => {
    const all = list.data?.pages.flatMap((p) => p.items) ?? [];
    // "mine" is filtered on the device (the server's `q` applies to the school list)
    return scope === "mine" && q ? all.filter((d) => (d.title || "").toLowerCase().includes(q.toLowerCase())) : all;
  }, [list.data, scope, q]);

  const fail = (e: unknown) => toast.error(describeError(e));

  return (
    <Page>
      <PageHeader title="Library" lede="Your lessons and units — and what colleagues share with your school."
        actions={<Link to="/"><Button variant="primary" icon={<Icon.Plus size={16} />}>New</Button></Link>} />

      <div className={s.bar}>
        <Segmented label="Whose" value={scope} onChange={setScope}
          options={[{ value: "mine", label: "My work" }, { value: "school", label: "Shared in my school" }]} />
        <Segmented label="Kind" value={kind} onChange={setKind}
          options={[{ value: "all", label: "All" }, { value: "lesson", label: "Lessons" }, { value: "unit", label: "Units" }]} />
        <div className={s.search}>
          <Icon.Search size={15} />
          <TextInput type="search" value={search} placeholder="Search titles" aria-label="Search titles" onChange={(e) => setSearch(e.target.value)} />
        </div>
      </div>

      {list.isPending ? (
        <div className={s.list}>{[0, 1, 2, 3].map((i) => <div key={i} className={s.row}><Skeleton w={36} h={36} /><div><Skeleton w="50%" /><Skeleton w="30%" h={10} style={{ marginTop: 6 }} /></div></div>)}</div>
      ) : list.error ? (
        <ErrorState error={describeError(list.error)} onRetry={() => list.refetch()} />
      ) : !items.length ? (
        q ? <EmptyState icon={<Icon.Search size={36} />} title="No matches">Nothing titled “{q}”.</EmptyState>
          : scope === "mine"
            ? <EmptyState icon={<Icon.LibraryBig size={40} />} title="Nothing saved yet" action={<Link to="/"><Button variant="primary">Make your first lesson</Button></Link>}>Lessons and units you make are saved here, with their full history.</EmptyState>
            : <EmptyState icon={<Icon.Users size={40} />} title="Nothing shared yet">When a colleague shares a plan with your school, it shows up here for you to view and copy.</EmptyState>
      ) : (
        <div className={s.list} role="list">
          {items.map((d) => {
            const mine = me?.id === d.owner_id;
            return (
              <div key={d.id} className={s.row} role="listitem">
                <span className={`${s.icon} ${d.kind === "unit" ? s.unit : ""}`} aria-hidden>{d.kind === "unit" ? <Icon.CalendarRange size={17} /> : <Icon.FileText size={17} />}</span>
                <div style={{ minWidth: 0 }}>
                  <Link to={href(d)} className={s.title}>{d.title || "Untitled"}</Link>
                  <div className={s.meta}>
                    <span>{d.kind === "unit" ? "Unit" : "Lesson"}</span>
                    {d.grade && <span>· Grade {d.grade}</span>}
                    {d.subject && <span>· {d.subject}</span>}
                    {scope === "school" && d.owner_name && <span>· by {d.owner_name}</span>}
                    {mine && d.visibility === "school" && <Badge tone="pine"><Icon.Globe size={10} /> Shared</Badge>}
                  </div>
                </div>
                <span className={s.when}>{relativeTime(d.updated_at)}</span>
                <Menu trigger={<IconButton label={`Actions for ${d.title || "document"}`}><Icon.MoreHorizontal size={18} /></IconButton>} items={[
                  { label: mine ? "Open" : "View", icon: <Icon.ExternalLink size={16} />, onSelect: () => navigate(href(d)) },
                  { label: "Download (.zip)", icon: <Icon.Download size={16} />, onSelect: () => act.exportZip.mutate(d, { onSuccess: (n) => toast.show(`Downloaded ${n}`), onError: fail }) },
                  ...(!mine ? [{ label: "Copy to my work", icon: <Icon.Copy size={16} />, onSelect: () => act.copy.mutate(d, { onSuccess: (c) => navigate(href(c.document)), onError: fail }) }] : []),
                  ...(mine && d.tenant_id ? [{
                    label: d.visibility === "school" ? "Make private" : "Share with my school", icon: d.visibility === "school" ? <Icon.Lock size={16} /> : <Icon.Share2 size={16} />,
                    onSelect: () => act.share.mutate({ doc: d, visibility: d.visibility === "school" ? "private" : "school" },
                      { onSuccess: (x) => toast.show(x.visibility === "school" ? "Shared — colleagues can view and copy it" : "Now private"), onError: fail }),
                  }] : []),
                  ...(mine ? ["separator" as const, { label: "Delete", danger: true, icon: <Icon.Trash2 size={16} />, onSelect: () => setDeleting(d) }] : []),
                ]} />
              </div>
            );
          })}
        </div>
      )}
      {list.hasNextPage && (
        <div style={{ display: "flex", justifyContent: "center", marginTop: 16 }}>
          <Button busy={list.isFetchingNextPage} onClick={() => list.fetchNextPage()}>Load more</Button>
        </div>
      )}

      <ConfirmDialog open={!!deleting} onOpenChange={(o) => !o && setDeleting(null)} danger busy={act.remove.isPending}
        title={`Delete “${deleting?.title || "Untitled"}”?`} confirmLabel="Delete for good"
        body="This deletes it and its whole history. Copies colleagues made are not affected. This can't be undone."
        onConfirm={() => act.remove.mutate(deleting!, { onSuccess: () => { toast.show("Deleted"); setDeleting(null); }, onError: (e) => { fail(e); setDeleting(null); } })} />
    </Page>
  );
}
