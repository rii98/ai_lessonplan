import type { BloomLevel } from "@/lib/api";
import { Button, Icon, SeamlessText, Select } from "@/ui";
import { issuesAt } from "../../model/issues";
import { BLOOM, blankObjective } from "../../model/ldd";
import { ItemCard, Issues, move, type SectionProps } from "../parts";
import s from "../editor.module.css";

export function ObjectivesSection({ lesson, edit, readOnly, issues }: SectionProps) {
  const ids = lesson.objectives.map((o) => o.id);
  const general = issues.filter((i) => i.path.length < 2);
  return (
    <>
      <Issues issues={general} />
      <div className={s.items}>
        {lesson.objectives.map((o, i) => {
          const own = issuesAt(issues, ["objectives", i]);
          const renameId = (next: string) => edit((d) => {
            const prev = d.objectives[i]!.id;
            d.objectives[i]!.id = next;
            // keep every link pointing at this objective
            for (const p of d.phases) p.objective_ids = (p.objective_ids ?? []).map((x) => (x === prev ? next : x));
            for (const q of d.formative_checks) q.objective_ids = q.objective_ids.map((x) => (x === prev ? next : x));
            if (d.homework) d.homework.objective_ids = (d.homework.objective_ids ?? []).map((x) => (x === prev ? next : x));
          }, `obj:${i}:id`);
          return (
            <ItemCard key={i} readOnly={readOnly} invalid={own.length > 0} removeLabel={`Remove objective ${o.id}`}
              onMove={(dir) => edit((d) => move(d.objectives, i, dir))}
              onRemove={() => edit((d) => {
                const gone = d.objectives[i]!.id;
                d.objectives.splice(i, 1);
                for (const p of d.phases) p.objective_ids = (p.objective_ids ?? []).filter((x) => x !== gone);
                for (const q of d.formative_checks) q.objective_ids = q.objective_ids.filter((x) => x !== gone);
              })}
              title={
                <div className={s.inline} style={{ alignItems: "center", gap: 8 }}>
                  <input aria-label="Objective id" value={o.id} readOnly={readOnly} size={4}
                    style={{ flex: "0 0 auto", width: "6ch", border: "1px solid var(--rule)", borderRadius: 6, padding: "2px 6px", background: "transparent", fontWeight: 700, color: "var(--pine)", fontSize: "var(--text-sm)" }}
                    onChange={(e) => renameId(e.target.value.replace(/\s+/g, ""))} />
                  <Select aria-label="Bloom level" className={s.compactSelect} value={o.bloom} disabled={readOnly} style={{ flex: "0 0 auto" }}
                    onChange={(e) => edit((d) => { d.objectives[i]!.bloom = e.target.value as BloomLevel; })}>
                    {BLOOM.map((b) => <option key={b} value={b}>{b[0]!.toUpperCase() + b.slice(1)}</option>)}
                  </Select>
                </div>
              }>
              <SeamlessText value={o.statement} readOnly={readOnly} invalid={own.length > 0} aria-label={`Objective ${o.id}`}
                placeholder="Students will be able to…" onValue={(v) => edit((d) => { d.objectives[i]!.statement = v; }, `obj:${i}`)} />
              <Issues issues={own} />
            </ItemCard>
          );
        })}
      </div>
      {!readOnly && (
        <div className={s.addRow}>
          <Button size="sm" icon={<Icon.Plus size={15} />} onClick={() => edit((d) => { d.objectives.push(blankObjective(ids)); })}>Add objective</Button>
        </div>
      )}
    </>
  );
}
