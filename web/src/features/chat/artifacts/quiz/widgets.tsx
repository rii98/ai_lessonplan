import { useState, type ReactNode } from "react";
import { Button, Icon, RichText, TextArea, TextInput } from "@/ui";
import type { Graded, PlayQuestion } from "./types";
import s from "./quiz.module.css";

export interface WidgetProps {
  q: PlayQuestion;
  /** Set once graded — the widget locks and shows right/wrong. */
  result: Graded | null;
  /** Previously submitted response (resumed attempt). */
  previous?: unknown;
  busy: boolean;
  submit: (response: unknown) => void;
}

function Choices({ options, result, picked, onPick, busy, isRight }: {
  options: { key: string; label: ReactNode; value: unknown }[]; result: Graded | null; picked: unknown; busy: boolean;
  onPick: (v: unknown) => void; isRight: (v: unknown) => boolean;
}) {
  return (
    <div className={s.opts} role="group">
      {options.map((o, i) => {
        // once graded: the right answer pops, a wrong pick is nudged, the rest step back
        const state = result ? (isRight(o.value) ? "ok" : picked === o.value ? "no" : "dim") : picked === o.value ? "pick" : undefined;
        return (
          <button key={o.key} type="button" className={s.opt} data-state={state} style={{ ["--i" as string]: i }} disabled={!!result || busy} onClick={() => onPick(o.value)}>
            <span className={s.optKey}>{o.key}</span><span className={s.optText}>{o.label}</span>
            <span className={s.optMark}>{state === "ok" && <Icon.CheckCircle2 size={20} aria-label="correct" />}{state === "no" && <Icon.RotateCcw size={18} aria-label="your pick" />}</span>
          </button>
        );
      })}
    </div>
  );
}

function ChoiceWidget({ q, result, previous, busy, submit }: WidgetProps) {
  const [picked, setPicked] = useState<unknown>(previous ?? null);
  const choices = q.choices ?? [];
  return <Choices busy={busy} result={result} picked={picked} isRight={(v) => choices[v as number] === result?.expected}
    onPick={(v) => { setPicked(v); submit(v); }}
    options={choices.map((c, i) => ({ key: String.fromCharCode(65 + i), label: <RichText text={c} />, value: i }))} />;
}

function BooleanWidget({ result, previous, busy, submit }: WidgetProps) {
  const [picked, setPicked] = useState<unknown>(previous ?? null);
  return <Choices busy={busy} result={result} picked={picked} isRight={(v) => String(v).toLowerCase() === String(result?.expected).toLowerCase()}
    onPick={(v) => { setPicked(v); submit(v); }}
    options={[{ key: "T", label: "True", value: "True" }, { key: "F", label: "False", value: "False" }]} />;
}

function TextWidget({ q, result, previous, busy, submit }: WidgetProps) {
  const [v, setV] = useState(typeof previous === "string" ? previous : "");
  const written = q.widget === "written";
  const go = () => v.trim() && submit(v.trim());
  return (
    <form className={s.answer} onSubmit={(e) => { e.preventDefault(); go(); }}>
      {written
        ? <TextArea autoGrow rows={3} value={v} disabled={!!result || busy} placeholder="Write your answer…" aria-label="Your answer" onChange={(e) => setV(e.target.value)} />
        : <TextInput autoFocus value={v} disabled={!!result || busy} inputMode={q.widget === "number" ? "decimal" : undefined} placeholder="Your answer" aria-label="Your answer" autoComplete="off" onChange={(e) => setV(e.target.value)} />}
      {!result && <div><Button type="submit" variant="primary" busy={busy} disabled={!v.trim()}>Check</Button></div>}
    </form>
  );
}

function MatchWidget({ q, result, previous, busy, submit }: WidgetProps) {
  const left = q.left ?? [], right = q.right ?? [];
  const [sel, setSel] = useState<string[]>(Array.isArray(previous) ? (previous as string[]) : left.map(() => ""));
  return (
    <div className={s.answer}>
      <div className={s.match}>
        {left.map((t, i) => (
          <label key={i} className={s.matchRow}>
            <span>{i + 1}. {t}</span>
            <select value={sel[i] ?? ""} disabled={!!result || busy} aria-label={`Match for ${t}`}
              onChange={(e) => setSel(sel.map((x, j) => (j === i ? e.target.value : x)))}>
              <option value="">—</option>
              {right.map((r) => <option key={r.letter} value={r.letter}>{r.letter}</option>)}
            </select>
          </label>
        ))}
      </div>
      <div className={s.rightCol}>{right.map((r) => <div key={r.letter}><b>{r.letter}.</b> {r.text}</div>)}</div>
      {!result && <div><Button variant="primary" busy={busy} disabled={sel.some((x) => !x)} onClick={() => submit(sel)}>Check</Button></div>}
    </div>
  );
}

function OrderWidget({ q, result, busy, submit }: WidgetProps) {
  const [items, setItems] = useState(q.items ?? []);
  const swap = (i: number, d: number) => { const j = i + d; if (j < 0 || j >= items.length) return; const n = [...items]; [n[i], n[j]] = [n[j]!, n[i]!]; setItems(n); };
  return (
    <div className={s.answer}>
      <p className={s.muted}>Put these in order:</p>
      <ol className={s.order}>
        {items.map((it, i) => (
          <li key={it.letter}>
            <span>{it.text}</span>
            {!result && <span style={{ display: "flex" }}>
              <button type="button" aria-label="Move up" onClick={() => swap(i, -1)} disabled={i === 0}><Icon.ArrowUp size={15} /></button>
              <button type="button" aria-label="Move down" onClick={() => swap(i, 1)} disabled={i === items.length - 1}><Icon.ArrowDown size={15} /></button>
            </span>}
          </li>
        ))}
      </ol>
      {!result && <div><Button variant="primary" busy={busy} onClick={() => submit(items.map((x) => x.letter))}>Check</Button></div>}
    </div>
  );
}

/** widget name (from the server's play view) → input. A new question widget is one entry. */
export const WIDGETS: Record<string, (p: WidgetProps) => JSX.Element> = {
  choice: ChoiceWidget, boolean: BooleanWidget, text: TextWidget, number: TextWidget, written: TextWidget, match: MatchWidget, order: OrderWidget,
};
