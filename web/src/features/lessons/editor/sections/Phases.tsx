import { Button, Icon, LineList, NumberInput, SeamlessText } from "@/ui";
import { issuesAt } from "../../model/issues";
import { blankPhase } from "../../model/ldd";
import { ItemCard, Issues, Label, move, ObjectiveLinks, PhaseTimeline, phaseColor, type SectionProps } from "../parts";
import s from "../editor.module.css";

export function PhasesSection({ lesson, edit, readOnly, issues }: SectionProps) {
  return (
    <>
      <div style={{ marginBottom: 16 }}><PhaseTimeline phases={lesson.phases} duration={lesson.duration_min} legend={false} /></div>
      <Issues issues={issues.filter((i) => i.path.length < 2)} />
      <div className={s.items}>
        {lesson.phases.map((p, i) => {
          const own = issuesAt(issues, ["phases", i]);
          return (
            <ItemCard key={i} readOnly={readOnly} invalid={own.length > 0} removeLabel={`Remove phase ${i + 1}`}
              no={<i className={s.swatch} style={{ background: phaseColor(i), width: 10, height: 10, display: "inline-block", borderRadius: 3 }} />}
              onMove={(dir) => edit((d) => move(d.phases, i, dir))}
              onRemove={lesson.phases.length > 1 ? () => edit((d) => { d.phases.splice(i, 1); }) : undefined}
              title={
                <div className={s.inline} style={{ alignItems: "center", gap: 10 }}>
                  <SeamlessText singleLine value={p.name_en} readOnly={readOnly} aria-label={`Phase ${i + 1} name`} placeholder="Phase name"
                    style={{ fontFamily: "var(--font-serif)", fontWeight: 600, flex: "2 1 160px" }}
                    onValue={(v) => edit((d) => { d.phases[i]!.name_en = v; }, `ph:${i}:n`)} />
                  <SeamlessText singleLine value={p.name_ne ?? ""} readOnly={readOnly} aria-label={`Phase ${i + 1} name in Nepali`} placeholder="नेपाली नाम"
                    style={{ color: "var(--ink-3)", flex: "1 1 100px" }}
                    onValue={(v) => edit((d) => { d.phases[i]!.name_ne = v; }, `ph:${i}:ne`)} />
                  <label style={{ display: "inline-flex", alignItems: "center", gap: 6, flex: "0 0 auto", fontSize: "var(--text-sm)", color: "var(--ink-3)" }}>
                    <NumberInput aria-label={`Phase ${i + 1} minutes`} min={1} max={120} value={p.minutes} readOnly={readOnly}
                      style={{ width: 64, minHeight: 30, padding: "3px 8px" }}
                      onValue={(v) => edit((d) => { d.phases[i]!.minutes = v ?? 0; }, `ph:${i}:m`)} />
                    min
                  </label>
                </div>
              }>
              <div className={s.cols}>
                <div>
                  <Label>Teacher does</Label>
                  <LineList label={`Teacher activities, phase ${i + 1}`} values={p.teacher_activities} readOnly={readOnly}
                    placeholder="What you do…" addLabel="Add a step" onChange={(v) => edit((d) => { d.phases[i]!.teacher_activities = v; })} />
                </div>
                <div>
                  <Label>Students do</Label>
                  <LineList label={`Student activities, phase ${i + 1}`} values={p.student_activities} readOnly={readOnly}
                    placeholder="What students do…" addLabel="Add a step" onChange={(v) => edit((d) => { d.phases[i]!.student_activities = v; })} />
                </div>
              </div>
              <Label>Teaches objectives</Label>
              <ObjectiveLinks objectives={lesson.objectives} value={p.objective_ids ?? []} readOnly={readOnly}
                onChange={(ids) => edit((d) => { d.phases[i]!.objective_ids = ids; })} />
              <Issues issues={own} />
            </ItemCard>
          );
        })}
      </div>
      {!readOnly && (
        <div className={s.addRow}>
          <Button size="sm" icon={<Icon.Plus size={15} />} onClick={() => edit((d) => { d.phases.push(blankPhase()); })}>Add phase</Button>
        </div>
      )}
    </>
  );
}
