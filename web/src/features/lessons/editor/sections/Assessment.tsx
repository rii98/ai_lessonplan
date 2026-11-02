import { Button, Icon, LineList } from "@/ui";
import { issuesAt } from "../../model/issues";
import { blankQuestion } from "../../model/ldd";
import { Issues, Label, move, ObjectiveLinks, type SectionProps } from "../parts";
import { QuestionEditor } from "../QuestionEditor";
import s from "../editor.module.css";

export function DifferentiationSection({ lesson, edit, readOnly, issues }: SectionProps) {
  const groups = [
    ["struggling", "Needs support", "A scaffold, a simpler entry point…"],
    ["on_level", "On level", "The core task…"],
    ["advanced", "Ready for more", "An extension or challenge…"],
  ] as const;
  return (
    <>
      <Issues issues={issues} />
      <div className={s.cols3}>
        {groups.map(([key, label, ph]) => (
          <div key={key}>
            <Label>{label}</Label>
            <LineList label={label} values={lesson.differentiation[key]} readOnly={readOnly} placeholder={ph} addLabel="Add"
              onChange={(v) => edit((d) => { d.differentiation[key] = v; })} />
          </div>
        ))}
      </div>
    </>
  );
}

export function ChecksSection({ lesson, edit, readOnly, issues }: SectionProps) {
  const assessed = new Set(lesson.formative_checks.flatMap((q) => q.objective_ids));
  const unassessed = lesson.objectives.filter((o) => !assessed.has(o.id));
  return (
    <>
      <Issues issues={issues.filter((i) => i.path.length < 2)} />
      <div className={s.items}>
        {lesson.formative_checks.map((q, i) => (
          <QuestionEditor key={i} q={q} no={i + 1} objectives={lesson.objectives} readOnly={readOnly}
            issues={issuesAt(issues, ["formative_checks", i]).map((x) => x.message)}
            update={(recipe, key) => edit((d) => recipe(d.formative_checks[i]!), key)}
            onMove={(dir) => edit((d) => move(d.formative_checks, i, dir))}
            onRemove={() => edit((d) => { d.formative_checks.splice(i, 1); })} />
        ))}
      </div>
      {!readOnly && (
        <div className={s.addRow}>
          <Button size="sm" icon={<Icon.Plus size={15} />}
            onClick={() => edit((d) => { d.formative_checks.push(blankQuestion(d.formative_checks.map((q) => q.id), "short_answer", unassessed[0] ? [unassessed[0].id] : [])); })}>
            Add check
          </Button>
          {unassessed.length > 0 && <span className={s.summary} style={{ alignSelf: "center" }}>Not yet checked: {unassessed.map((o) => o.id).join(", ")}</span>}
        </div>
      )}
    </>
  );
}

export function HomeworkSection({ lesson, edit, readOnly, issues }: SectionProps) {
  const hw = lesson.homework;
  if (!hw) {
    return readOnly ? <p className={s.summary}>No homework.</p> : (
      <Button size="sm" icon={<Icon.Plus size={15} />} onClick={() => edit((d) => { d.homework = { instructions: [""], objective_ids: [] }; })}>Add homework</Button>
    );
  }
  return (
    <>
      <Issues issues={issues} />
      <LineList numbered label="Homework tasks" values={hw.instructions} readOnly={readOnly} placeholder="A task to do at home…" addLabel="Add a task"
        onChange={(v) => edit((d) => { d.homework!.instructions = v; })} />
      <Label>Practises objectives</Label>
      <ObjectiveLinks objectives={lesson.objectives} value={hw.objective_ids ?? []} readOnly={readOnly}
        onChange={(ids) => edit((d) => { d.homework!.objective_ids = ids; })} />
      {!readOnly && (
        <div className={s.addRow}>
          <Button size="sm" variant="quiet" icon={<Icon.Trash2 size={15} />} onClick={() => edit((d) => { d.homework = null; })}>Remove homework</Button>
        </div>
      )}
    </>
  );
}
