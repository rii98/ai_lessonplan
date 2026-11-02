import { useState, type ChangeEvent } from "react";
import { describeError } from "@/lib/api";
import { breadcrumb } from "@/lib/format";
import { Banner, Button, Dialog, Field, FieldGrid, Icon, NumberInput, Segmented, Select, TextArea, TextInput } from "@/ui";
import { parseJsonl, useChunkers, useCorpusMutations, type DocumentIngest } from "./api";
import s from "./knowledge.module.css";

type Tab = "book" | "note" | "bulk";

const readFile = async (e: ChangeEvent<HTMLInputElement>) => (await e.target.files?.[0]?.text()) ?? "";

/** Add material to the selected shelf: a whole book chapter, one note, or many records. */
export function AddMaterial({ open, onOpenChange, space, spaceName, collections }: {
  open: boolean; onOpenChange: (o: boolean) => void; space: string; spaceName: string; collections: string[];
}) {
  const [tab, setTab] = useState<Tab>("book");
  const [done, setDone] = useState<string | null>(null);
  return (
    <Dialog wide open={open} onOpenChange={(o) => { onOpenChange(o); if (!o) setDone(null); }} title="Add material" description={`Goes onto the “${spaceName}” shelf.`}>
      <div style={{ display: "grid", gap: 16 }}>
        <Segmented label="What are you adding" value={tab} onChange={(t) => { setTab(t); setDone(null); }} options={[
          { value: "book", label: "A book chapter" }, { value: "note", label: "One note" }, { value: "bulk", label: "Many records (JSONL)" },
        ]} />
        {done && <Banner tone="success" title="Added">{done}</Banner>}
        {tab === "book" && <BookForm space={space} collections={collections} onDone={setDone} />}
        {tab === "note" && <NoteForm space={space} collections={collections} onDone={setDone} />}
        {tab === "bulk" && <BulkForm space={space} collections={collections} onDone={setDone} />}
      </div>
    </Dialog>
  );
}

function CollectionSelect({ value, onChange, collections }: { value: string; onChange: (v: string) => void; collections: string[] }) {
  return (
    <Field label="Collection">
      <Select value={value} onChange={(e) => onChange(e.target.value)}>
        {collections.map((c) => <option key={c} value={c}>{c.replace("_", " ")}</option>)}
      </Select>
    </Field>
  );
}

function BookForm({ space, collections, onDone }: { space: string; collections: string[]; onDone: (m: string) => void }) {
  const m = useCorpusMutations(space);
  const chunkers = useChunkers().data;
  const [f, setF] = useState({ collection: collections.includes("reference") ? "reference" : collections[0] ?? "", grade: null as number | null, subject: "", topic: "", source: "", text: "",
    chunker: "", maxChars: 1200, minChars: null as number | null, depth: 6, chapterLevel: "", breadcrumb: true, heading: true });
  const set = <K extends keyof typeof f>(key: K, v: (typeof f)[K]) => setF({ ...f, [key]: v });
  const req = (): DocumentIngest => ({
    collection: f.collection, text: f.text, format: "markdown", source: f.source.trim() || null,
    chunker: f.chunker || chunkers?.by_format?.markdown || null, space,
    metadata: { ...(f.grade ? { grade: f.grade } : {}), ...(f.subject.trim() ? { subject: f.subject.trim() } : {}), ...(f.topic.trim() ? { topic: f.topic.trim() } : {}) },
    params: { max_chars: f.maxChars || 1200, prepend_breadcrumb: f.breadcrumb, include_heading: f.heading,
      split_levels: Array.from({ length: f.depth }, (_, i) => i + 1), ...(f.minChars ? { min_chars: f.minChars } : {}), ...(f.chapterLevel ? { chapter_level: Number(f.chapterLevel) } : {}) },
  });
  const err = m.preview.error ?? m.ingestDocument.error;
  return (
    <div style={{ display: "grid", gap: 14 }}>
      <p style={{ fontSize: "var(--text-sm)", color: "var(--ink-2)" }}>Paste or upload a Markdown chapter. It's split at headings and every piece remembers where it came from (“Chapter 2 › Biotic components”). Optional YAML front-matter (grade, subject, source) is read automatically.</p>
      <FieldGrid>
        <CollectionSelect value={f.collection} onChange={(v) => set("collection", v)} collections={collections} />
        <Field label="Grade" optional><NumberInput min={1} max={12} value={f.grade} onValue={(v) => set("grade", v)} /></Field>
        <Field label="Subject" optional><TextInput value={f.subject} onChange={(e) => set("subject", e.target.value)} /></Field>
        <Field label="Topic" optional><TextInput value={f.topic} onChange={(e) => set("topic", e.target.value)} /></Field>
      </FieldGrid>
      <Field label="Source label" hint="What teachers see as provenance, e.g. “Science Grade 10 — Chapter 4”"><TextInput value={f.source} onChange={(e) => set("source", e.target.value)} /></Field>
      <Field label="Markdown">
        <TextArea className={s.code} rows={10} value={f.text} placeholder={"# Chapter 4 — Electricity\n\n## Current\n\n…"} onChange={(e) => set("text", e.target.value)} />
      </Field>
      <input type="file" accept=".md,.markdown,.txt" aria-label="Upload a Markdown file"
        onChange={async (e) => { const name = e.target.files?.[0]?.name; const t = await readFile(e); setF({ ...f, text: t, source: f.source || name || "" }); }} />
      <details>
        <summary style={{ cursor: "pointer", fontSize: "var(--text-sm)", fontWeight: 600, color: "var(--ink-2)" }}>How it's split</summary>
        <div style={{ display: "grid", gap: 12, marginTop: 12 }}>
          <FieldGrid>
            <Field label="Strategy"><Select value={f.chunker} onChange={(e) => set("chunker", e.target.value)}><option value="">Default</option>{chunkers?.chunkers.map((c) => <option key={c} value={c}>{c}</option>)}</Select></Field>
            <Field label="Max characters a piece"><NumberInput min={100} value={f.maxChars} onValue={(v) => set("maxChars", v ?? 1200)} /></Field>
            <Field label="Merge sections under" optional><NumberInput min={0} value={f.minChars} placeholder="off" onValue={(v) => set("minChars", v)} /></Field>
            <Field label="Split down to">
              <Select value={f.depth} onChange={(e) => set("depth", Number(e.target.value))}>{[1, 2, 3, 4, 5, 6].map((d) => <option key={d} value={d}>{d === 6 ? "every heading" : `H1–H${d}`}</option>)}</Select>
            </Field>
            <Field label="Chapter heading" hint="Auto-detect works for most books.">
              <Select value={f.chapterLevel} onChange={(e) => set("chapterLevel", e.target.value)}>
                <option value="">Auto-detect</option><option value="1">H1 is a chapter</option><option value="2">H2 is a chapter</option><option value="3">H3 is a chapter</option>
              </Select>
            </Field>
          </FieldGrid>
          <div style={{ display: "flex", gap: 16, flexWrap: "wrap" }}>
            <label className={s.check}><input type="checkbox" checked={f.breadcrumb} onChange={(e) => set("breadcrumb", e.target.checked)} /> Put the heading trail in each piece</label>
            <label className={s.check}><input type="checkbox" checked={f.heading} onChange={(e) => set("heading", e.target.checked)} /> Keep the heading line</label>
          </div>
        </div>
      </details>
      {err && <Banner tone="danger">{describeError(err)}</Banner>}
      <div style={{ display: "flex", gap: 8 }}>
        <Button disabled={!f.text.trim()} busy={m.preview.isPending} icon={<Icon.Layers size={15} />} onClick={() => m.preview.mutate(req())}>Preview the split</Button>
        <Button variant="primary" disabled={!f.text.trim()} busy={m.ingestDocument.isPending} icon={<Icon.Upload size={15} />}
          onClick={() => m.ingestDocument.mutate(req(), { onSuccess: (r) => { onDone(`${r.chunks} pieces from “${f.source || "the chapter"}” added to ${f.collection}. Adding the same book again updates it in place.`); setF({ ...f, text: "" }); m.preview.reset(); } })}>Add the chapter</Button>
      </div>
      {m.preview.data && (
        <div style={{ display: "grid", gap: 8 }}>
          <div style={{ fontSize: "var(--text-sm)", fontWeight: 600 }}>{m.preview.data.total} pieces{m.preview.data.truncated ? ` — first ${m.preview.data.chunks.length} shown` : ""}</div>
          {m.preview.data.chunks.map((c, i) => (
            <div key={i} className={s.chunk}>
              <div className={s.chunkHead}><span>{i + 1}. {breadcrumb(c.heading_path || c.heading) || "(before the first heading)"}</span><span>{c.chars} chars</span></div>
              <div className={s.chunkText}>{c.text}</div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function NoteForm({ space, collections, onDone }: { space: string; collections: string[]; onDone: (m: string) => void }) {
  const m = useCorpusMutations(space);
  const [f, setF] = useState({ collection: collections.includes("pedagogical") ? "pedagogical" : collections[0] ?? "", text: "", grade: null as number | null, subject: "", topic: "", standard: "", language: "", framework: "", source: "" });
  const set = <K extends keyof typeof f>(key: K, v: (typeof f)[K]) => setF({ ...f, [key]: v });
  const record = () => Object.fromEntries(Object.entries({ text: f.text.trim(), grade: f.grade, subject: f.subject, topic: f.topic, standard: f.standard, language: f.language, framework: f.framework, source: f.source })
    .filter(([, v]) => v !== null && String(v).trim() !== "").map(([k, v]) => [k, typeof v === "string" ? v.trim() : v]));
  return (
    <div style={{ display: "grid", gap: 14 }}>
      <FieldGrid>
        <CollectionSelect value={f.collection} onChange={(v) => set("collection", v)} collections={collections} />
        <Field label="Grade" optional><NumberInput min={1} max={12} value={f.grade} onValue={(v) => set("grade", v)} /></Field>
        <Field label="Subject" optional><TextInput value={f.subject} onChange={(e) => set("subject", e.target.value)} /></Field>
        <Field label="Topic" optional><TextInput value={f.topic} onChange={(e) => set("topic", e.target.value)} /></Field>
        <Field label="Standard / code" optional><TextInput value={f.standard} onChange={(e) => set("standard", e.target.value)} /></Field>
        <Field label="Framework" optional hint="Exemplars only">
          <Select value={f.framework} onChange={(e) => set("framework", e.target.value)}><option value="">Any</option><option value="5E">5E</option><option value="gradual_release">Gradual release</option><option value="inquiry">Inquiry</option></Select>
        </Field>
      </FieldGrid>
      <Field label="Text" hint="What the AI will ground on."><TextArea rows={5} value={f.text} placeholder="e.g. Students often think soil is living because plants grow in it; soil is abiotic, though full of living things." onChange={(e) => set("text", e.target.value)} /></Field>
      <Field label="Source label" optional><TextInput value={f.source} placeholder="e.g. CDC Science Grade 6 — misconception bank" onChange={(e) => set("source", e.target.value)} /></Field>
      {m.ingest.error && <Banner tone="danger">{describeError(m.ingest.error)}</Banner>}
      <div><Button variant="primary" disabled={!f.text.trim()} busy={m.ingest.isPending}
        onClick={() => m.ingest.mutate({ collection: f.collection, records: [record()] }, { onSuccess: (r) => { onDone(`Saved — ${r.chunks} searchable piece${r.chunks === 1 ? "" : "s"}. The same text again updates it.`); set("text", ""); } })}>Add note</Button></div>
    </div>
  );
}

function BulkForm({ space, collections, onDone }: { space: string; collections: string[]; onDone: (m: string) => void }) {
  const m = useCorpusMutations(space);
  const [collection, setCollection] = useState(collections.includes("pedagogical") ? "pedagogical" : collections[0] ?? "");
  const [text, setText] = useState("");
  const [parseError, setParseError] = useState<string | null>(null);
  const submit = () => {
    let records;
    try { records = parseJsonl(text); setParseError(null); } catch (e) { setParseError((e as Error).message); return; }
    m.ingest.mutate({ collection, records }, { onSuccess: (r) => { onDone(`${r.documents} records → ${r.chunks} searchable pieces.`); setText(""); } });
  };
  return (
    <div style={{ display: "grid", gap: 14 }}>
      <p style={{ fontSize: "var(--text-sm)", color: "var(--ink-2)" }}>One JSON object per line, each with a <code>"text"</code>. A line's own <code>"collection"</code> wins over the default.</p>
      <div style={{ maxWidth: 280 }}><CollectionSelect value={collection} onChange={setCollection} collections={collections} /></div>
      <TextArea className={s.code} rows={8} value={text} aria-label="Records (JSONL)" placeholder={'{"text": "…", "grade": 6, "subject": "Science"}'} onChange={(e) => setText(e.target.value)} />
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
        <input type="file" accept=".jsonl,.json,.txt" aria-label="Upload a JSONL file" onChange={async (e) => setText(await readFile(e))} />
        <Button size="sm" variant="quiet" onClick={() => setText([
          '{"text": "Students often think soil is biotic because plants grow in it; soil is abiotic.", "collection": "pedagogical", "grade": 6, "subject": "Science", "source": "Misconception bank"}',
          '{"text": "Frogs, snails and herons in the paddy fields form a simple food chain.", "collection": "local_context", "grade": 6, "subject": "Science"}',
        ].join("\n"))}>Load an example</Button>
      </div>
      {(parseError || m.ingest.error) && <Banner tone="danger">{parseError ?? describeError(m.ingest.error)}</Banner>}
      <div><Button variant="primary" disabled={!text.trim()} busy={m.ingest.isPending} onClick={submit}>Add records</Button></div>
    </div>
  );
}
