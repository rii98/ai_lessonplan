import { Button, ChipInput, Icon, SeamlessText } from "@/ui";
import { issuesAt } from "../../model/issues";
import { blankMisconception, type Lesson } from "../../model/ldd";
import { ItemCard, Issues, type SectionProps } from "../parts";
import s from "../editor.module.css";

type ChipKey = "prior_knowledge" | "local_context" | "materials";

/** A section that is one list of short items (chips). */
export function chipSection(key: ChipKey, placeholder: string) {
  return function ChipSection({ lesson, edit, readOnly, issues }: SectionProps) {
    return (
      <>
        <Issues issues={issues} />
        <ChipInput label={key.replace("_", " ")} values={lesson[key] as Lesson[ChipKey]} readOnly={readOnly} placeholder={placeholder}
          onChange={(v) => edit((d) => { d[key] = v; })} />
      </>
    );
  };
}

export function MisconceptionsSection({ lesson, edit, readOnly, issues }: SectionProps) {
  return (
    <>
      <Issues issues={issues.filter((i) => i.path.length < 2)} />
      <div className={s.items}>
        {lesson.misconceptions.map((m, i) => (
          <ItemCard key={i} readOnly={readOnly} removeLabel={`Remove misconception ${i + 1}`} invalid={issuesAt(issues, ["misconceptions", i]).length > 0}
            onRemove={() => edit((d) => { d.misconceptions.splice(i, 1); })}>
            <div className={s.pair}>
              <div className={s.pairRow}>
                <span className={`${s.pairKey} ${s.wrong}`}>They think</span>
                <SeamlessText value={m.statement} readOnly={readOnly} aria-label="Misconception" placeholder="What students wrongly believe…"
                  onValue={(v) => edit((d) => { d.misconceptions[i]!.statement = v; }, `mis:${i}:s`)} />
              </div>
              <div className={s.pairRow}>
                <span className={`${s.pairKey} ${s.right}`}>Correct by</span>
                <SeamlessText value={m.correction} readOnly={readOnly} aria-label="Correction" placeholder="How to show them otherwise…"
                  onValue={(v) => edit((d) => { d.misconceptions[i]!.correction = v; }, `mis:${i}:c`)} />
              </div>
            </div>
            {m.source && <p className={s.summary} style={{ marginTop: 6 }}>From: {m.source}</p>}
          </ItemCard>
        ))}
      </div>
      {!readOnly && (
        <div className={s.addRow}>
          <Button size="sm" icon={<Icon.Plus size={15} />} onClick={() => edit((d) => { d.misconceptions.push(blankMisconception()); })}>Add misconception</Button>
        </div>
      )}
    </>
  );
}
