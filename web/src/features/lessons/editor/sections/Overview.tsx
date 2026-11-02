import { Field, FieldGrid, NumberInput, Segmented, Select, TextInput } from "@/ui";
import { DURATIONS, FRAMEWORKS, LANGUAGES, type Lesson } from "../../model/ldd";
import { Issues, type SectionProps } from "../parts";

export function OverviewSection({ lesson, edit, readOnly, issues }: SectionProps) {
  const ref = lesson.curriculum_ref;
  return (
    <>
      <Issues issues={issues} />
      <FieldGrid>
        <Field label="Grade">
          <NumberInput min={1} max={12} value={ref.grade} readOnly={readOnly}
            onValue={(v) => edit((d) => { d.curriculum_ref.grade = v ?? 0; }, "grade")} />
        </Field>
        <Field label="Subject">
          <TextInput value={ref.subject} readOnly={readOnly}
            onChange={(e) => edit((d) => { d.curriculum_ref.subject = e.target.value; }, "subject")} />
        </Field>
        <Field label="Language">
          <Select value={lesson.language} disabled={readOnly}
            onChange={(e) => edit((d) => { d.language = e.target.value as Lesson["language"]; })}>
            {LANGUAGES.map((l) => <option key={l.value} value={l.value}>{l.label}</option>)}
          </Select>
        </Field>
        <Field label="Framework">
          <Select value={lesson.framework} disabled={readOnly}
            onChange={(e) => edit((d) => { d.framework = e.target.value as Lesson["framework"]; })}>
            {FRAMEWORKS.map((f) => <option key={f.value} value={f.value}>{f.label}</option>)}
          </Select>
        </Field>
      </FieldGrid>
      <div style={{ marginTop: 14 }}>
        <div style={{ fontSize: "var(--text-sm)", fontWeight: 560, color: "var(--ink-2)", marginBottom: 6 }}>Period length</div>
        <Segmented label="Period length" value={String(lesson.duration_min)}
          onChange={(v) => !readOnly && edit((d) => { d.duration_min = Number(v) as Lesson["duration_min"]; })}
          options={DURATIONS.map((m) => ({ value: String(m), label: `${m} min`, disabled: readOnly }))} />
      </div>
    </>
  );
}
