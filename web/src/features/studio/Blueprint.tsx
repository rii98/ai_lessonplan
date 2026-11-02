import type { AssessmentSpec, TypeSpec } from "@/lib/api";
import { plural } from "@/lib/format";
import { Banner, Button, Field, Muted, Row, Select } from "@/ui";
import { useRetrievalPlan } from "./api";
import type { QuestionCatalog } from "./types";
import s from "./studio.module.css";

const GRANULARITY: Record<string, string> = { narrow: "focused passages", section: "whole sections", broad: "whole chapters" };

/** The blueprint's problem, if any — the server enforces the same limits. */
export function specProblem(spec: AssessmentSpec, cat: QuestionCatalog): string | null {
  if (!spec.types.length) return "Pick at least one question type.";
  for (const t of spec.types) {
    if (!Number.isInteger(t.count) || t.count < 1 || t.count > cat.limits.max_per_type) return `Each type needs 1–${cat.limits.max_per_type} questions.`;
  }
  const total = spec.types.reduce((a, t) => a + t.count, 0);
  return total > cat.limits.max_total ? `That's ${total} questions — the limit is ${cat.limits.max_total}.` : null;
}

/** Catalogue order (objective types first) — what's sent and saved. */
export function ordered(spec: AssessmentSpec, cat: QuestionCatalog): AssessmentSpec {
  const idx = (t: string) => cat.types.findIndex((m) => m.type === t);
  return { ...spec, types: [...spec.types].sort((a, b) => idx(a.type) - idx(b.type)).map((t) => ({ ...t, instruction: t.instruction.trim() })) };
}

/** Choose question types → how many, how hard, marks, an instruction per type. */
export function Blueprint({ spec, onChange, catalog, days }: {
  spec: AssessmentSpec; onChange: (s: AssessmentSpec) => void; catalog: QuestionCatalog;
  days?: { all: { n: number; topic: string }[]; selected: number[]; onChange: (d: number[]) => void };
}) {
  const problem = specProblem(spec, catalog);
  const plan = useRetrievalPlan(problem ? null : ordered(spec, catalog));
  const total = spec.types.reduce((a, t) => a + (t.count || 0), 0);
  const marks = spec.types.reduce((a, t) => a + (t.count || 0) * (t.marks_each ?? 0), 0);
  const setType = (type: string, patch: Partial<TypeSpec>) =>
    onChange({ ...spec, types: spec.types.map((t) => (t.type === type ? { ...t, ...patch } : t)) });
  const scope = catalog.scopes.find((x) => x.value === spec.scope);

  return (
    <div style={{ display: "grid", gap: 20 }}>
      {days && (
        <div>
          <Muted>Days to cover — questions are tied to each chosen day's objectives and spread across them.</Muted>
          <div className={s.days} style={{ marginTop: 8 }}>
            {days.all.map((d) => (
              <label key={d.n} className={s.dayChip} title={d.topic}>
                <input type="checkbox" checked={days.selected.includes(d.n)}
                  onChange={(e) => days.onChange(e.target.checked ? [...days.selected, d.n].sort((a, b) => a - b) : days.selected.filter((x) => x !== d.n))} />
                Day {d.n}
              </label>
            ))}
          </div>
        </div>
      )}

      <Row gap={6}>
        <span style={{ fontSize: "var(--text-sm)", color: "var(--ink-3)" }}>Start from</span>
        {catalog.presets.map((p) => (
          <Button key={p.label} size="sm" variant="quiet" onClick={() => onChange({ ...spec, types: p.types.map((t) => ({ ...t, instruction: t.instruction ?? "" })) })}>{p.label}</Button>
        ))}
        <Button size="sm" variant="quiet" onClick={() => onChange({ ...spec, types: [] })}>Clear</Button>
      </Row>

      <div className={s.typeGrid}>
        {catalog.types.map((m) => {
          const row = spec.types.find((t) => t.type === m.type);
          return (
            <div key={m.type} className={s.type} data-on={!!row}>
              <label className={s.typeHead}>
                <input type="checkbox" checked={!!row} onChange={(e) => onChange({
                  ...spec, types: e.target.checked
                    ? [...spec.types, { type: m.type, count: m.default_count, difficulty: "mixed", marks_each: m.default_marks, instruction: "" }]
                    : spec.types.filter((t) => t.type !== m.type),
                })} />
                <span><div className={s.typeLabel}>{m.label}</div><div className={s.typeHint}>{m.hint}</div></span>
              </label>
              {row && (<>
                <div className={s.typeCtl}>
                  <label>How many<input type="number" min={1} max={catalog.limits.max_per_type} value={row.count || ""}
                    onChange={(e) => setType(m.type, { count: Number(e.target.value) })} /></label>
                  <label>Difficulty<select value={row.difficulty} onChange={(e) => setType(m.type, { difficulty: e.target.value as TypeSpec["difficulty"] })}>
                    {catalog.difficulties.map((d) => <option key={d} value={d}>{d}</option>)}</select></label>
                  <label>Marks each<input type="number" min={0} value={row.marks_each ?? ""} placeholder="—"
                    onChange={(e) => setType(m.type, { marks_each: e.target.value === "" ? null : Number(e.target.value) })} /></label>
                </div>
                <input className={s.instr} maxLength={500} value={row.instruction} aria-label={`Instruction for ${m.label}`}
                  placeholder="Optional — e.g. use prices from the local haat bazaar"
                  onChange={(e) => setType(m.type, { instruction: e.target.value })} />
              </>)}
            </div>
          );
        })}
      </div>

      <div style={{ display: "grid", gap: 10 }}>
        <Field label="Draw from the textbook" hint={scope?.help}>
          <Select value={spec.scope} onChange={(e) => onChange({ ...spec, scope: e.target.value as AssessmentSpec["scope"] })} style={{ maxWidth: 320 }}>
            {catalog.scopes.map((x) => <option key={x.value} value={x.value}>{x.label}</option>)}
          </Select>
        </Field>
        <div className={s.totals}>
          <span><b>{total}</b>{plural(total, "question").replace(/^\d+ /, "")}</span>
          {marks > 0 && <span><b>{marks}</b>marks</span>}
          <span>{plural(spec.types.length, "type")}</span>
        </div>
        {problem ? <Banner tone="warning">{problem}</Banner> : plan.data && (
          <ul className={s.plan} aria-label="How it will be grounded">
            {plan.data.passes.map((p, i) => <li key={i}>Looks up <b>{GRANULARITY[p.granularity] ?? p.granularity}</b> (top {p.top_n}) — {p.why}</li>)}
          </ul>
        )}
      </div>
    </div>
  );
}
