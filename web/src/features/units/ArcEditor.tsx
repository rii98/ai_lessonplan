import type { Draft } from "immer";
import { move } from "@/features/lessons";
import type { UnitPlan } from "@/lib/api";
import { Button, Icon, IconButton, LineList, SeamlessText } from "@/ui";
import s from "./units.module.css";

/** The unit's spine — every field editable; this exact object drives the expansion. */
export function ArcEditor({ plan, edit, disabled }: {
  plan: UnitPlan; edit: (recipe: (d: Draft<UnitPlan>) => void, key?: string) => void; disabled?: boolean;
}) {
  const renumber = (d: Draft<UnitPlan>) => d.days.forEach((x, i) => { x.day = i + 1; });
  return (
    <div className={s.arc}>
      <div>
        <div className={s.lbl}>Unit title</div>
        <SeamlessText singleLine value={plan.title} readOnly={disabled} aria-label="Unit title"
          style={{ fontFamily: "var(--font-serif)", fontSize: "var(--text-2xl)", fontWeight: 600 }}
          onValue={(v) => edit((d) => { d.title = v; }, "title")} />
      </div>
      <div className={s.links}>
        <div>
          <div className={s.lbl}>Big idea</div>
          <SeamlessText value={plan.big_idea ?? ""} readOnly={disabled} aria-label="Big idea" placeholder="The one understanding students leave with"
            onValue={(v) => edit((d) => { d.big_idea = v; }, "big")} />
        </div>
        <div>
          <div className={s.lbl}>Running thread</div>
          <SeamlessText value={plan.thread ?? ""} readOnly={disabled} aria-label="Running thread" placeholder="A worked example carried across days"
            onValue={(v) => edit((d) => { d.thread = v; }, "thread")} />
        </div>
      </div>
      <div>
        <div className={s.lbl}>Unit outcomes</div>
        <LineList label="Unit outcomes" values={plan.unit_outcomes ?? []} readOnly={disabled} placeholder="By the end of the unit, students can…" addLabel="Add an outcome"
          onChange={(v) => edit((d) => { d.unit_outcomes = v; })} />
      </div>

      <div className={s.lbl} style={{ marginTop: 8 }}>Day by day</div>
      {plan.days.map((day, i) => (
        <div key={i} className={s.day}>
          <div className={s.dayNo}><span>Day</span><b>{i + 1}</b></div>
          <div className={s.dayBody}>
            <div className={s.dayHead}>
              <SeamlessText singleLine className={s.dayTopic} value={day.topic} readOnly={disabled} aria-label={`Day ${i + 1} topic`} placeholder="What this day teaches"
                onValue={(v) => edit((d) => { d.days[i]!.topic = v; }, `d${i}t`)} />
              {!disabled && (
                <span style={{ display: "flex" }}>
                  <IconButton size="sm" label="Move day earlier" disabled={i === 0} onClick={() => edit((d) => { move(d.days, i, -1); renumber(d); })}><Icon.ArrowUp size={15} /></IconButton>
                  <IconButton size="sm" label="Move day later" disabled={i === plan.days.length - 1} onClick={() => edit((d) => { move(d.days, i, 1); renumber(d); })}><Icon.ArrowDown size={15} /></IconButton>
                  <IconButton size="sm" label={`Remove day ${i + 1}`} disabled={plan.days.length <= 1} onClick={() => edit((d) => { d.days.splice(i, 1); renumber(d); })}><Icon.Trash2 size={15} /></IconButton>
                </span>
              )}
            </div>
            <LineList label={`Day ${i + 1} objectives`} values={day.objective_seeds ?? []} readOnly={disabled} placeholder="An objective for this day" addLabel="Add an objective"
              onChange={(v) => edit((d) => { d.days[i]!.objective_seeds = v; })} />
            <div className={s.links}>
              <div>
                <div className={s.lbl}>Builds on</div>
                <SeamlessText className={s.small} value={day.builds_on ?? ""} readOnly={disabled} aria-label={`Day ${i + 1} builds on`} placeholder="What it picks up from before"
                  onValue={(v) => edit((d) => { d.days[i]!.builds_on = v; }, `d${i}b`)} />
              </div>
              <div>
                <div className={s.lbl}>Sets up</div>
                <SeamlessText className={s.small} value={day.sets_up ?? ""} readOnly={disabled} aria-label={`Day ${i + 1} sets up`} placeholder="What it prepares for next"
                  onValue={(v) => edit((d) => { d.days[i]!.sets_up = v; }, `d${i}s`)} />
              </div>
            </div>
          </div>
        </div>
      ))}
      {!disabled && (
        <div>
          <Button size="sm" icon={<Icon.Plus size={15} />}
            onClick={() => edit((d) => { d.days.push({ day: d.days.length + 1, topic: "", objective_seeds: [], builds_on: "", sets_up: "" }); })}>Add a day</Button>
        </div>
      )}
    </div>
  );
}
