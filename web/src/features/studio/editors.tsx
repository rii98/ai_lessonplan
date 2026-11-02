import type { Draft } from "immer";
import { BLOOM, blankQuestion, FieldLabel, ItemCard, move, QuestionEditor } from "@/features/lessons";
import type { BloomLevel, Objective, Question } from "@/lib/api";
import { Button, Icon, LineList, SeamlessText, Select } from "@/ui";
import type { Artifact, Quiz, Slides, Worksheet } from "./types";
import s from "./studio.module.css";

export interface ArtifactEditorProps<T> {
  value: T;
  edit: (recipe: (d: Draft<T>) => void, key?: string) => void;
  onRevise: (target: string) => void;
}

const ReviseBtn = ({ onClick, label }: { onClick: () => void; label: string }) =>
  <Button size="sm" variant="ai" icon={<Icon.PenLine size={14} />} onClick={onClick} aria-label={`Revise ${label} with AI`}>Revise</Button>;

function Heading({ title, onRevise }: { title: string; onRevise?: () => void }) {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 10, margin: "24px 0 10px" }}>
      <h2 style={{ fontSize: "var(--text-xl)" }}>{title}</h2>
      {onRevise && <span style={{ marginLeft: "auto" }}><ReviseBtn onClick={onRevise} label={title} /></span>}
    </div>
  );
}

function ObjectivesBlock<T extends { objectives: Objective[] }>({ value, edit, onRevise }: ArtifactEditorProps<T>) {
  return (<>
    <Heading title="Objectives" onRevise={() => onRevise("objectives")} />
    <div style={{ display: "grid", gap: 10 }}>
      {value.objectives.map((o, i) => (
        <ItemCard key={i} removeLabel={`Remove objective ${o.id}`} onRemove={value.objectives.length > 1 ? () => edit((d) => { d.objectives.splice(i, 1); }) : undefined}
          title={<span style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <b style={{ color: "var(--pine)", fontSize: "var(--text-sm)" }}>{o.id}</b>
            <Select aria-label="Bloom level" value={o.bloom} style={{ width: "auto", minHeight: 30, padding: "3px 30px 3px 8px", fontSize: "var(--text-sm)" }}
              onChange={(e) => edit((d) => { d.objectives[i]!.bloom = e.target.value as BloomLevel; })}>
              {BLOOM.map((b) => <option key={b} value={b}>{b}</option>)}
            </Select>
          </span>}>
          <SeamlessText value={o.statement} aria-label={`Objective ${o.id}`} onValue={(v) => edit((d) => { d.objectives[i]!.statement = v; }, `o${i}`)} />
        </ItemCard>
      ))}
    </div>
  </>);
}

function QuestionsBlock<T extends { questions: Question[]; objectives: Objective[] }>({ value, edit, onRevise }: ArtifactEditorProps<T>) {
  return (<>
    <Heading title="Questions" onRevise={() => onRevise("questions")} />
    <div style={{ display: "grid", gap: 10 }}>
      {value.questions.map((q, i) => (
        <QuestionEditor key={i} q={q} no={i + 1} objectives={value.objectives}
          update={(recipe, key) => edit((d) => recipe(d.questions[i]! as Draft<Question>), key)}
          onMove={(dir) => edit((d) => move(d.questions, i, dir))}
          onRemove={() => edit((d) => { d.questions.splice(i, 1); })} />
      ))}
    </div>
    <div style={{ marginTop: 10 }}>
      <Button size="sm" icon={<Icon.Plus size={15} />} onClick={() => edit((d) => {
        d.questions.push(blankQuestion(d.questions.map((q) => q.id), "short_answer", d.objectives[0] ? [d.objectives[0].id] : []));
      })}>Add question</Button>
    </div>
  </>);
}

function Header<T extends { topic: string; curriculum_ref: { grade: number; subject: string } }>({ value, edit }: Pick<ArtifactEditorProps<T>, "value" | "edit">) {
  return (
    <SeamlessText value={value.topic} aria-label="Topic" onValue={(v) => edit((d) => { d.topic = v; }, "topic")}
      style={{ fontFamily: "var(--font-serif)", fontSize: "var(--text-2xl)", fontWeight: 600 }} />
  );
}

export function QuizEditor(p: ArtifactEditorProps<Quiz>) {
  return (<>
    <Header {...p} />
    <Heading title="Instructions for students" onRevise={() => p.onRevise("instructions")} />
    <SeamlessText value={p.value.instructions} aria-label="Instructions" placeholder="e.g. Answer every question. Show your working."
      onValue={(v) => p.edit((d) => { d.instructions = v; }, "instr")} />
    <ObjectivesBlock {...p} />
    <QuestionsBlock {...p} />
  </>);
}

export function WorksheetEditor(p: ArtifactEditorProps<Worksheet>) {
  return (<>
    <Header {...p} />
    <ObjectivesBlock {...p} />
    <Heading title="Tasks" onRevise={() => p.onRevise("tasks")} />
    <LineList numbered label="Tasks" values={p.value.tasks} placeholder="A hands-on task…" addLabel="Add a task" onChange={(v) => p.edit((d) => { d.tasks = v; })} />
    <QuestionsBlock {...p} />
  </>);
}

export function SlidesEditor({ value, edit, onRevise }: ArtifactEditorProps<Slides>) {
  return (<>
    <Header value={value} edit={edit} />
    <FieldLabel>Subtitle</FieldLabel>
    <SeamlessText value={value.subtitle} aria-label="Subtitle" onValue={(v) => edit((d) => { d.subtitle = v; }, "sub")} />
    <Heading title="Slides" onRevise={() => onRevise("slides")} />
    <div style={{ display: "grid", gap: 10 }}>
      {value.slides.map((sl, i) => (
        <ItemCard key={i} no={<span className={s.slideNo}>{i + 1}</span>} removeLabel={`Remove slide ${i + 1}`}
          onMove={(dir) => edit((d) => move(d.slides, i, dir))}
          onRemove={value.slides.length > 1 ? () => edit((d) => { d.slides.splice(i, 1); }) : undefined}
          title={<SeamlessText singleLine value={sl.heading} aria-label={`Slide ${i + 1} heading`} placeholder="Heading"
            onValue={(v) => edit((d) => { d.slides[i]!.heading = v; }, `h${i}`)} />}>
          <SeamlessText singleLine value={sl.subtitle ?? ""} aria-label={`Slide ${i + 1} subtitle`} placeholder="Subtitle (optional)"
            style={{ color: "var(--ink-3)" }} onValue={(v) => edit((d) => { d.slides[i]!.subtitle = v || null; }, `s${i}`)} />
          <div style={{ marginTop: 8 }}>
            <LineList label={`Slide ${i + 1} bullets`} values={sl.bullets} placeholder="A point" addLabel="Add a bullet" onChange={(v) => edit((d) => { d.slides[i]!.bullets = v; })} />
          </div>
        </ItemCard>
      ))}
    </div>
    <div style={{ marginTop: 10 }}>
      <Button size="sm" icon={<Icon.Plus size={15} />} onClick={() => edit((d) => { d.slides.push({ heading: "", subtitle: null, bullets: [""] }); })}>Add slide</Button>
    </div>
  </>);
}

export type AnyEditor = (p: ArtifactEditorProps<Artifact>) => JSX.Element;
