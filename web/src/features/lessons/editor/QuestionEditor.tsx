import type { Draft } from "immer";
import type { Difficulty, Objective, Question, QuestionType } from "@/lib/api";
import { ChipInput, Icon, LineList, SeamlessText, Select, TextInput } from "@/ui";
import { QUESTION_TYPE_ORDER, QUESTION_TYPES } from "../model/questions";
import { ItemCard, Label, ObjectiveLinks } from "./parts";
import s from "./editor.module.css";

export type QuestionUpdate = (recipe: (q: Draft<Question>) => void, key?: string) => void;

const DIFFICULTIES: (Difficulty | "")[] = ["", "easy", "medium", "hard"];

/**
 * One question of any type. Shared by the lesson's formative checks and the
 * quiz/worksheet studio — the per-type rules live in `QUESTION_TYPES`, so a new
 * type is a data change there plus (if it has new structure) one block below.
 */
export function QuestionEditor({ q, no, update, objectives, readOnly, issues = [], onRemove, onMove }: {
  q: Question; no: number; update: QuestionUpdate; objectives: Objective[]; readOnly?: boolean;
  issues?: string[]; onRemove?: () => void; onMove?: (dir: -1 | 1) => void;
}) {
  const meta = QUESTION_TYPES[q.type];
  const k = (field: string) => `q:${no}:${field}`;

  const changeType = (type: QuestionType) => update((d) => {
    d.type = type;
    const m = QUESTION_TYPES[type];
    if (m.options && !d.options?.length) d.options = ["", ""];
    if (m.pairs && (d.pairs?.length ?? 0) < 2) d.pairs = [{ left: "", right: "" }, { left: "", right: "" }];
    if (type === "true_false" && !/^(true|false)$/i.test(d.answer)) d.answer = "True";
  });

  return (
    <ItemCard no={`${no}.`} invalid={issues.length > 0} readOnly={readOnly} onRemove={onRemove} onMove={onMove} removeLabel={`Remove question ${no}`}
      title={
        <div className={s.inline} style={{ gap: 8, alignItems: "center" }}>
          <Select aria-label="Question type" className={s.compactSelect} value={q.type} disabled={readOnly} style={{ flex: "0 0 auto" }}
            onChange={(e) => changeType(e.target.value as QuestionType)}>
            {QUESTION_TYPE_ORDER.map((t) => <option key={t} value={t}>{QUESTION_TYPES[t].label}</option>)}
          </Select>
          <Select aria-label="Difficulty" className={s.compactSelect} value={q.difficulty ?? ""} disabled={readOnly} style={{ flex: "0 0 auto" }}
            onChange={(e) => update((d) => { d.difficulty = (e.target.value || null) as Difficulty | null; })}>
            {DIFFICULTIES.map((d) => <option key={d} value={d}>{d || "Any difficulty"}</option>)}
          </Select>
          <TextInput aria-label="Marks" type="number" min={0} placeholder="marks" className={s.compactSelect} style={{ width: 84, flex: "0 0 84px" }}
            value={q.marks ?? ""} readOnly={readOnly}
            onChange={(e) => update((d) => { d.marks = e.target.value === "" ? null : Number(e.target.value); }, k("marks"))} />
        </div>
      }>
      <SeamlessText value={q.prompt} readOnly={readOnly} placeholder={q.type === "fill_blank" ? "The ____ is where photosynthesis happens." : "Write the question…"}
        aria-label={`Question ${no}`} style={{ fontWeight: 520 }}
        onValue={(v) => update((d) => { d.prompt = v; }, k("prompt"))} />

      {meta.options === "choices" && (<>
        <Label>Options</Label>
        <LineList label={`Options for question ${no}`} values={q.options ?? []} readOnly={readOnly} addLabel="Add an option" placeholder="An option"
          onChange={(v) => update((d) => { d.options = v; })} />
      </>)}
      {meta.options === "steps" && (<>
        <Label>Steps — in the correct order (the sheet scrambles them)</Label>
        <LineList numbered label={`Steps for question ${no}`} values={q.options ?? []} readOnly={readOnly} addLabel="Add a step" placeholder="A step"
          onChange={(v) => update((d) => { d.options = v; })} />
      </>)}
      {meta.pairs && (<>
        <Label>Pairs — the right column is scrambled on the sheet</Label>
        <div style={{ display: "grid", gap: 6 }}>
          {(q.pairs ?? []).map((p, i) => (
            <div key={i} style={{ display: "grid", gridTemplateColumns: "1fr 1fr auto", gap: 8, alignItems: "center" }}>
              <TextInput aria-label={`Left ${i + 1}`} value={p.left} readOnly={readOnly} placeholder="Item"
                onChange={(e) => update((d) => { d.pairs![i]!.left = e.target.value; }, k(`pl${i}`))} />
              <TextInput aria-label={`Right ${i + 1}`} value={p.right} readOnly={readOnly} placeholder="Its match"
                onChange={(e) => update((d) => { d.pairs![i]!.right = e.target.value; }, k(`pr${i}`))} />
              {!readOnly && <button type="button" aria-label={`Remove pair ${i + 1}`} style={{ border: 0, background: "none", color: "var(--ink-3)", cursor: "pointer" }}
                onClick={() => update((d) => { d.pairs!.splice(i, 1); })}><Icon.X size={15} /></button>}
            </div>
          ))}
          {!readOnly && <button type="button" className={s.suggestion} style={{ justifySelf: "start" }}
            onClick={() => update((d) => { (d.pairs ??= []).push({ left: "", right: "" }); })}>+ Add a pair</button>}
        </div>
      </>)}

      {!meta.derivedAnswer && (<>
        <Label>Answer{meta.answerHint && <span style={{ textTransform: "none", letterSpacing: 0, fontWeight: 400 }}>· {meta.answerHint}</span>}</Label>
        {q.type === "true_false" ? (
          <Select aria-label="Answer" value={/^false$/i.test(q.answer) ? "False" : "True"} disabled={readOnly} style={{ maxWidth: 160 }}
            onChange={(e) => update((d) => { d.answer = e.target.value; })}>
            <option>True</option><option>False</option>
          </Select>
        ) : q.type === "mcq" && (q.options ?? []).some(Boolean) ? (
          <Select aria-label="Correct option" value={q.answer} disabled={readOnly}
            onChange={(e) => update((d) => { d.answer = e.target.value; })}>
            <option value="">Choose the correct option…</option>
            {(q.options ?? []).filter(Boolean).map((o, i) => <option key={i} value={o}>{o}</option>)}
          </Select>
        ) : (
          <SeamlessText value={q.answer} readOnly={readOnly} placeholder="The model answer" aria-label="Answer"
            onValue={(v) => update((d) => { d.answer = v; }, k("answer"))} />
        )}
      </>)}
      {meta.derivedAnswer && <p className={s.summary} style={{ marginTop: 8 }}>The answer key is computed from the {meta.pairs ? "pairs" : "steps"}.</p>}

      {meta.keyPoints && (<>
        <Label>Marking points</Label>
        <ChipInput label={`Marking points for question ${no}`} values={q.key_points ?? []} readOnly={readOnly} placeholder="A point worth a mark"
          onChange={(v) => update((d) => { d.key_points = v; })} />
      </>)}

      <Label>Checks objectives</Label>
      <ObjectiveLinks objectives={objectives} value={q.objective_ids ?? []} readOnly={readOnly}
        onChange={(ids) => update((d) => { d.objective_ids = ids; })} />

      {issues.map((m, i) => <p key={i} className={s.issue}><Icon.AlertTriangle size={14} aria-hidden /> {m}</p>)}
    </ItemCard>
  );
}
