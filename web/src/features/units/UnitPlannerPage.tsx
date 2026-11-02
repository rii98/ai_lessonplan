import { useEffect, useRef, useState } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { SourcesTree } from "@/features/grounding";
import { FRAMEWORKS, LANGUAGES } from "@/features/lessons";
import { useProfile } from "@/features/profile";
import { describeError, type CoverageReport, type UnitPlan } from "@/lib/api";
import { plural } from "@/lib/format";
import { useDraft, useElapsed, useUnsavedGuard } from "@/lib/hooks";
import {
  Banner, Button, ConfirmDialog, Field, FieldGrid, Icon, NumberInput, Page, PageHeader, Panel, ProgressPanel, Section, Select, TextArea, TextInput,
} from "@/ui";
import { useGenerateUnit, usePlanUnit } from "./api";
import { ArcEditor } from "./ArcEditor";
import { missingFields, toRequest, type UnitBrief, type UnitPlannerState } from "./brief";
import { Coverage } from "./Coverage";
import s from "./units.module.css";

const DEFAULT: UnitBrief = { topic: "", grade: null, subject: "", num_days: 5, duration_min: 45, language: "en-ne", framework: "5E" };

/**
 * `/units/new` — brief → editable arc (one cheap planner call) → write every day.
 * The teacher's edits to the arc are exactly what gets expanded.
 */
export function UnitPlannerPage() {
  const location = useLocation();
  const handoff = (location.state ?? {}) as UnitPlannerState;
  const profile = useProfile().data;
  const [brief, setBrief] = useState<UnitBrief>(() => ({ ...DEFAULT, ...handoff.brief }));
  const plan = usePlanUnit();
  const [planned, setPlanned] = useState<{ plan: UnitPlan; coverage: CoverageReport; brief: UnitBrief; gen: number } | null>(null);
  const [error, setError] = useState<string | null>(null);

  // profile defaults fill only what the teacher (or Home) left blank
  useEffect(() => {
    if (!profile) return;
    setBrief((b) => ({
      ...b,
      grade: b.grade ?? profile.default_grade ?? null,
      subject: b.subject || profile.default_subject || "",
    }));
  }, [profile]);

  const runPlan = (b = brief) => {
    const missing = missingFields(b);
    if (missing.length) { setError(`Add the ${missing.join(", ")} first.`); return; }
    setError(null);
    plan.mutate(toRequest(b), {
      onSuccess: (r) => setPlanned((p) => ({ plan: r.plan, coverage: r.coverage, brief: b, gen: (p?.gen ?? 0) + 1 })),
      onError: (e) => setError(describeError(e)),
    });
  };

  // Home hands over a complete brief with `autoplan` — start planning straight away (once)
  const autoplanned = useRef(false);
  useEffect(() => {
    if (handoff.autoplan && !autoplanned.current && !missingFields(brief).length) { autoplanned.current = true; runPlan(brief); }
  });

  const briefChanged = planned && JSON.stringify(toRequest(planned.brief)) !== JSON.stringify(toRequest(brief));
  const elapsed = useElapsed(plan.isPending);
  const set = <K extends keyof UnitBrief>(k: K, v: UnitBrief[K]) => setBrief((b) => ({ ...b, [k]: v }));

  return (
    <Page width="wide">
      <PageHeader eyebrow={<Link to="/units"><Icon.ArrowLeft size={13} /> Units</Link>}
        title="Plan a unit" lede="Spread a chapter across several days as one arc — each day builds on the last. Shape the arc first (quick), then write every lesson." />

      <Section title="1 · The brief">
        <div style={{ display: "grid", gap: 16 }}>
          <Field label="What do you want to teach?" hint="A chapter, a topic, or a fuller prompt.">
            <TextArea autoGrow rows={2} value={brief.topic} placeholder="e.g. Electricity and magnetism — start from a load-shedding story, end with a circuit they build"
              onChange={(e) => set("topic", e.target.value)} />
          </Field>
          <FieldGrid>
            <Field label="Grade"><NumberInput min={1} max={12} value={brief.grade} onValue={(v) => set("grade", v)} /></Field>
            <Field label="Subject"><TextInput value={brief.subject} onChange={(e) => set("subject", e.target.value)} placeholder="Science" /></Field>
            <Field label="Days"><NumberInput min={1} max={20} value={brief.num_days} onValue={(v) => set("num_days", v ?? 5)} /></Field>
            <Field label="Minutes a day">
              <Select value={brief.duration_min} onChange={(e) => set("duration_min", Number(e.target.value))}>
                {[30, 45, 60].map((m) => <option key={m} value={m}>{m} min</option>)}
              </Select>
            </Field>
            <Field label="Language">
              <Select value={brief.language} onChange={(e) => set("language", e.target.value as UnitBrief["language"])}>
                {LANGUAGES.map((l) => <option key={l.value} value={l.value}>{l.label}</option>)}
              </Select>
            </Field>
            <Field label="Framework">
              <Select value={brief.framework} onChange={(e) => set("framework", e.target.value as UnitBrief["framework"])}>
                {FRAMEWORKS.map((f) => <option key={f.value} value={f.value}>{f.label}</option>)}
              </Select>
            </Field>
          </FieldGrid>
          {error && <Banner tone="danger">{error}</Banner>}
          {plan.isPending
            ? <ProgressPanel title="Shaping the arc" elapsed={elapsed} stages={[
                { label: "Reading the chapter", detail: "finding its topics in your knowledge base" },
                { label: "Planning", detail: `spreading it over ${plural(brief.num_days, "day")}, each building on the last` },
              ]} note="One quick AI call — usually under 30 seconds." />
            : <div><Button variant={planned ? "default" : "primary"} icon={<Icon.CalendarRange size={16} />} onClick={() => runPlan()}>{planned ? "Re-plan from this brief" : "Plan the arc"}</Button></div>}
        </div>
      </Section>

      {planned && (
        <ArcStep key={planned.gen} initial={planned.plan} coverage={planned.coverage} brief={planned.brief} briefChanged={!!briefChanged}
          title={brief.title} onReplan={() => runPlan()} />
      )}
    </Page>
  );
}

function ArcStep({ initial, coverage, brief, briefChanged, title, onReplan }: {
  initial: UnitPlan; coverage: CoverageReport; brief: UnitBrief; briefChanged: boolean; title?: string; onReplan: () => void;
}) {
  const navigate = useNavigate();
  const draft = useDraft<UnitPlan>(initial);
  const generate = useGenerateUnit();
  const elapsed = useElapsed(generate.isPending);
  const blocker = useUnsavedGuard(!generate.isSuccess);
  const plan = draft.value;
  const days = plan.days.length;
  const blank = plan.days.some((d) => !d.topic.trim());

  const write = () => generate.mutate(
    { ...toRequest(brief), num_days: days, title: title || plan.title || null, plan },
    { onSuccess: (saved) => navigate(`/units/${saved.document.id}`, { replace: true }) },
  );

  return (
    <Section title="2 · The arc" hint="Edit anything — titles, order, objectives. Your edits drive how every day is written.">
      {briefChanged && <div style={{ marginBottom: 12 }}><Banner tone="warning" title="The brief changed" actions={<Button size="sm" onClick={onReplan}>Re-plan</Button>}>This arc was planned for the previous brief.</Banner></div>}
      <div className={s.split}>
        <ArcEditor plan={plan} edit={draft.update} disabled={generate.isPending} />
        <aside className={s.aside}>
          <Panel><div className={s.lbl} style={{ marginBottom: 8 }}>Syllabus coverage</div>
            {coverage.topics?.length ? <Coverage report={coverage} plan={plan} /> : <p className={s.small} style={{ color: "var(--ink-3)" }}>No book for this chapter is in the knowledge base, so coverage can't be checked.</p>}
          </Panel>
          <Panel><div className={s.lbl} style={{ marginBottom: 8 }}>Grounded in</div>
            <SourcesTree query={{ topic: brief.topic, grade: brief.grade, subject: brief.subject, framework: brief.framework }} />
          </Panel>
        </aside>
      </div>
      <div className={s.stickyCta}>
        {generate.isPending ? (
          <div style={{ flex: 1 }}>
            <ProgressPanel title={`Writing ${plural(days, "lesson")}`} elapsed={elapsed}
              stages={[{ label: "Each day", detail: "grounded in the chapter, then drafted, critiqued and revised" },
                { label: "The whole unit", detail: "checked for coherence across days" }]}
              note="This is the slow step — often a few minutes. Keep this tab open; the unit is saved to your library when it's done." />
          </div>
        ) : (<>
          <span className={s.small} style={{ color: "var(--ink-2)" }}>{plural(days, "day")} × {brief.duration_min} min · {brief.subject}, grade {brief.grade}</span>
          <Button variant="primary" icon={<Icon.PenLine size={16} />} disabled={blank} title={blank ? "Every day needs a topic" : undefined} onClick={write}>Write all {plural(days, "lesson")}</Button>
        </>)}
        {generate.error && <div style={{ flexBasis: "100%" }}><Banner tone="danger" title="Couldn't write the unit">{describeError(generate.error)} Your arc is still here — try again.</Banner></div>}
      </div>
      <ConfirmDialog open={blocker.state === "blocked"} onOpenChange={(o) => { if (!o) blocker.reset?.(); }}
        title="Leave the planner?" body={generate.isPending ? "The unit is still being written. Leaving now may lose it." : "Your arc isn't saved until you write the lessons."}
        confirmLabel="Leave" danger onConfirm={() => blocker.proceed?.()} />
    </Section>
  );
}
