import { useState } from "react";
import { describeError } from "@/lib/api";
import { useStoredState } from "@/lib/hooks";
import {
  Badge, Banner, Button, ConfirmDialog, Dialog, EmptyState, ErrorState, Field, Icon, IconButton, Page, PageHeader, Section, Select, Skeleton, SkeletonLines, TextInput, useToast,
} from "@/ui";
import { AddMaterial } from "./AddMaterial";
import { useCorpusMutations, useOverview, useRecords, useSpaces, type CorpusRecord } from "./api";
import s from "./knowledge.module.css";

const SCHOOL_ID = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/;

/** `/corpus` — the grounding material every lesson draws on. */
export function KnowledgePage() {
  const toast = useToast();
  const spaces = useSpaces();
  const [space, setSpace] = useStoredState("lf.corpus.space", "global");
  const [extraSpaces, setExtra] = useState<string[]>([]);
  const overview = useOverview(space);
  const [browse, setBrowse] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [newShelf, setNewShelf] = useState<string | null>(null);
  const muts = useCorpusMutations(space);
  const [deleting, setDeleting] = useState<CorpusRecord | null>(null);
  const records = useRecords(space, browse);

  const shelves = [...(spaces.data ?? []), ...extraSpaces.filter((x) => !spaces.data?.some((sp) => sp.id === x)).map((id) => ({ id, name: `School ${id.replace("school:", "")}`, writable: true }))];
  const current = shelves.find((x) => x.id === space);
  const collections = overview.data?.map((c) => c.name) ?? [];
  const items = records.data?.pages.flatMap((p) => p.items) ?? [];

  return (
    <Page>
      <PageHeader title="Knowledge base" lede="The textbooks, standards and teaching notes every lesson is grounded in. The better this is, the less generic every lesson becomes."
        actions={<Button variant="primary" icon={<Icon.Plus size={16} />} disabled={!collections.length || current?.writable === false} onClick={() => setAdding(true)}>Add material</Button>} />

      <div style={{ display: "flex", gap: 12, alignItems: "flex-end", flexWrap: "wrap", marginBottom: 20 }}>
        <Field label="Shelf" hint="The shared shelf is visible to every school; a school's shelf only to that school.">
          <Select value={space} style={{ minWidth: 260 }} onChange={(e) => { if (e.target.value === "__new__") setNewShelf(""); else { setSpace(e.target.value); setBrowse(null); } }}>
            {shelves.map((x) => <option key={x.id} value={x.id} disabled={!x.writable}>{x.name}</option>)}
            <option value="__new__">+ A school's private shelf…</option>
          </Select>
        </Field>
      </div>

      <Section title="Collections" hint="Choose one to browse what's in it.">
        {overview.isPending ? <div className={s.cols}>{[0, 1, 2, 3].map((i) => <div key={i} className={s.col}><Skeleton w="60%" /><Skeleton h={10} /></div>)}</div>
          : overview.error ? <ErrorState error={`${describeError(overview.error)} Is the vector store running?`} onRetry={() => overview.refetch()} />
          : (
            <div className={s.cols}>
              {overview.data.map((c) => (
                <button key={c.name} type="button" className={s.col} aria-pressed={browse === c.name} onClick={() => setBrowse(browse === c.name ? null : c.name)}>
                  <span className={s.colHead}><span className={s.colName}>{c.name.replace("_", " ")}</span><span className={s.count}>{c.count}</span></span>
                  <span className={s.desc}>{c.description}</span>
                </button>
              ))}
            </div>
          )}
      </Section>

      {browse && (
        <Section title={<span style={{ textTransform: "capitalize" }}>{browse.replace("_", " ")}</span>} hint={current?.name}
          actions={<Button size="sm" variant="quiet" onClick={() => setBrowse(null)}>Close</Button>}>
          {records.isPending ? <SkeletonLines lines={5} /> : records.error ? <ErrorState error={describeError(records.error)} onRetry={() => records.refetch()} />
            : !items.length ? <EmptyState icon={<Icon.BookOpen size={36} />} title="Empty" action={<Button onClick={() => setAdding(true)}>Add material</Button>}>Nothing on this shelf in this collection yet.</EmptyState>
            : (
              <div className={s.records}>
                {items.map((r) => (
                  <div key={r.id} className={s.rec}>
                    <div className={s.recText}>{r.text}</div>
                    <div className={s.recMeta}>
                      {r.source && <span>{r.source}</span>}
                      {Object.entries(r.metadata ?? {}).filter(([k]) => !k.startsWith("_") && k !== "source").slice(0, 6).map(([k, v]) => <Badge key={k}>{k}: {String(v)}</Badge>)}
                      <span style={{ marginLeft: "auto" }}><IconButton size="sm" label="Delete record" onClick={() => setDeleting(r)}><Icon.Trash2 size={15} /></IconButton></span>
                    </div>
                  </div>
                ))}
                {records.hasNextPage && <div><Button busy={records.isFetchingNextPage} onClick={() => records.fetchNextPage()}>Load more</Button></div>}
              </div>
            )}
        </Section>
      )}

      <Section title="How grounding works">
        <div style={{ display: "grid", gap: 8, fontSize: "var(--text-sm)", color: "var(--ink-2)", maxWidth: "70ch" }}>
          <p><b>Reference</b> holds the course books: when a topic is covered there, lessons ground in that book rather than the model's general knowledge. <b>Curriculum</b> is what students must learn; <b>pedagogical</b> is how to teach it (misconceptions, strategies); <b>exemplar</b> is whole model lessons, tagged with their framework; <b>local context</b> is the places, crops and festivals that make a lesson familiar.</p>
          <p>Material is keyed by its content: adding the same text again updates it in place rather than duplicating it.</p>
        </div>
      </Section>

      {current && <AddMaterial open={adding} onOpenChange={setAdding} space={space} spaceName={current.name} collections={collections} />}

      <Dialog open={newShelf !== null} onOpenChange={(o) => !o && setNewShelf(null)} title="A school's private shelf"
        description="Only that school's teachers and students will see what you add here."
        footer={<><Button variant="quiet" onClick={() => setNewShelf(null)}>Cancel</Button>
          <Button variant="primary" disabled={!SCHOOL_ID.test(newShelf ?? "")} onClick={() => {
            const id = `school:${newShelf}`; setExtra((x) => [...x, id]); setSpace(id); setBrowse(null); setNewShelf(null);
          }}>Open shelf</Button></>}>
        <Field label="School id" hint="As Edboox sends it, e.g. sch-7"><TextInput autoFocus value={newShelf ?? ""} onChange={(e) => setNewShelf(e.target.value.trim())} /></Field>
      </Dialog>

      <ConfirmDialog open={!!deleting} onOpenChange={(o) => !o && setDeleting(null)} danger busy={muts.remove.isPending}
        title="Delete this record?" confirmLabel="Delete" body={<p style={{ fontSize: "var(--text-sm)", color: "var(--ink-2)" }}>{deleting?.text.slice(0, 220)}</p>}
        onConfirm={() => muts.remove.mutate({ collection: browse!, id: deleting!.id }, {
          onSuccess: () => { toast.show("Deleted"); setDeleting(null); },
          onError: (e) => toast.error(describeError(e)),
        })} />
      {spaces.error && <Banner tone="warning">Couldn't load the list of shelves: {describeError(spaces.error)}</Banner>}
    </Page>
  );
}
