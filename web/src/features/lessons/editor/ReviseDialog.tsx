import { useEffect, useRef, useState } from "react";
import type { RubricScores } from "@/lib/api";
import { describeError } from "@/lib/api";
import { humanize } from "@/lib/format";
import { useElapsed } from "@/lib/hooks";
import { Banner, Button, Dialog, Icon, ProgressPanel, TextArea } from "@/ui";
import s from "./editor.module.css";

/** What any "revise with AI" endpoint returns (lesson and artifact refine share it). */
export interface Proposal<T> {
  ok: boolean;
  candidate?: T | null;
  diff?: Record<string, { before?: unknown; after?: unknown }>;
  cascaded?: string[];
  errors?: string[];
  score_before?: RubricScores | null;
  score_after?: RubricScores | null;
}

const SUGGESTIONS = [
  "Use examples from my students' daily life",
  "Simpler language for this grade",
  "Add a hands-on activity",
  "Make it shorter and sharper",
  "Make it more challenging",
];

const fmt = (v: unknown) => (v == null ? "—" : typeof v === "string" ? v : JSON.stringify(v, null, 2));

export interface ReviseDialogProps<T> {
  open: boolean;
  onOpenChange: (o: boolean) => void;
  /** What is being revised, e.g. "Hook" or "the whole lesson". */
  label: string;
  context: string;
  /** Ask the server for a proposal (never applies anything). */
  propose: (instruction: string) => Promise<Proposal<T>>;
  /** The draft revision now; a proposal computed against an older one is stale. */
  revision: number;
  onAccept: (candidate: T) => void;
  sectionLabel?: (key: string) => string;
}

/**
 * The AI-revision loop: instruction → proposal → review (before/after per section,
 * score delta) → accept or discard. Nothing changes until the teacher accepts, and
 * a proposal made before newer edits is flagged as stale.
 */
export function ReviseDialog<T>({ open, onOpenChange, label, context, propose, revision, onAccept, sectionLabel = humanize }: ReviseDialogProps<T>) {
  const [instruction, setInstruction] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<{ p: Proposal<T>; at: number } | null>(null);
  const elapsed = useElapsed(busy);
  const ctl = useRef<{ cancelled: boolean }>({ cancelled: false });

  useEffect(() => {
    if (open) return;
    ctl.current.cancelled = true;
    setInstruction(""); setResult(null); setError(null); setBusy(false);
  }, [open]);

  const run = async () => {
    const text = instruction.trim();
    if (!text) { setError("Tell the AI what to change."); return; }
    const token = { cancelled: false };
    ctl.current = token;
    setBusy(true); setError(null); setResult(null);
    const at = revision;
    try {
      const p = await propose(text);
      if (!token.cancelled) setResult({ p, at });
    } catch (e) {
      if (!token.cancelled) setError(describeError(e));
    } finally {
      if (!token.cancelled) setBusy(false);
    }
  };

  const p = result?.p;
  const stale = result != null && result.at !== revision;
  const changes = Object.entries(p?.diff ?? {});

  return (
    <Dialog wide open={open} onOpenChange={onOpenChange} title={<>Revise {label}</>}
      description={`The AI reads ${context} for context, re-checks your reference material and rewrites only this part. You review the change before anything is applied.`}
      footer={p?.ok && p.candidate && changes.length ? (<>
        <Button variant="quiet" onClick={() => setResult(null)}>Try a different instruction</Button>
        <Button variant="quiet" onClick={() => onOpenChange(false)}>Discard</Button>
        <Button variant="primary" icon={<Icon.Check size={16} />} onClick={() => { onAccept(p.candidate as T); onOpenChange(false); }}>Accept change</Button>
      </>) : (<>
        <Button variant="quiet" onClick={() => onOpenChange(false)}>Cancel</Button>
        <Button variant="ai" busy={busy} icon={<Icon.PenLine size={16} />} onClick={run}>{busy ? "Revising…" : "Propose a change"}</Button>
      </>)}>
      {!p && (<>
        <TextArea autoFocus value={instruction} rows={3} disabled={busy} aria-label="What should change?"
          placeholder="e.g. make the hook about the stream behind the school, and end it with a prediction"
          onChange={(e) => setInstruction(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) void run(); }} />
        <div className={s.suggestions}>
          {SUGGESTIONS.map((t) => <button key={t} type="button" className={s.suggestion} disabled={busy} onClick={() => setInstruction(t)}>{t}</button>)}
        </div>
        {busy && <div style={{ marginTop: 16 }}><ProgressPanel title="Writing a proposal" elapsed={elapsed} note="Usually 10–40 seconds." /></div>}
      </>)}
      {error && <div style={{ marginTop: 12 }}><Banner tone="danger" title="The revision didn't go through">{error}</Banner></div>}

      {p && !p.ok && (
        <Banner tone="warning" title="The AI couldn't make a valid change"
          actions={<Button size="sm" onClick={() => setResult(null)}>Rephrase</Button>}>
          <ul style={{ margin: "4px 0 0", paddingLeft: 18 }}>{(p.errors?.length ? p.errors : ["No usable change came back."]).map((e, i) => <li key={i}>{e}</li>)}</ul>
        </Banner>
      )}

      {p?.ok && (
        <div className={s.proposal}>
          {stale && <Banner tone="warning" title="You've edited since this was proposed">Accepting replaces the whole draft with this proposal, including your newer edits.</Banner>}
          {p.score_before && p.score_after && (
            <div className={s.delta} aria-label="Score change">
              {(Object.keys(p.score_before) as (keyof RubricScores)[]).map((k) => {
                const b = p.score_before![k], a = p.score_after![k];
                const dir = a > b + 1e-9 ? s.up : a < b - 1e-9 ? s.down : "";
                return <span key={k} className={`${s.deltaItem} ${dir}`}>{humanize(k)} {b.toFixed(2)} → {a.toFixed(2)}</span>;
              })}
            </div>
          )}
          {!!p.cascaded?.length && <p className={s.summary}>Also adjusted so the lesson stays valid: {p.cascaded.map(sectionLabel).join(", ")}.</p>}
          {!changes.length && <Banner tone="info">The AI returned no changes. Try a more specific instruction.</Banner>}
          {changes.map(([key, d]) => (
            <div key={key} className={s.change}>
              <div className={s.changeHead}>{sectionLabel(key)}</div>
              <div className={s.changeBody}>
                <pre className={s.before} aria-label="Before">{fmt(d.before)}</pre>
                <pre className={s.after} aria-label="After">{fmt(d.after)}</pre>
              </div>
            </div>
          ))}
        </div>
      )}
    </Dialog>
  );
}
