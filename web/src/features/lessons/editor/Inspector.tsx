import { useState } from "react";
import { SourcesTree } from "@/features/grounding";
import { describeError, type Critique, type RubricScores } from "@/lib/api";
import { dateTime, humanize } from "@/lib/format";
import { Banner, Button, ErrorState, Icon, Meter, Muted, Sheet, SkeletonLines, Spinner, Stack } from "@/ui";
import { useCritique, useMoveHead, useVersions, type LiveValidation } from "../api";
import type { Lesson } from "../model/ldd";
import { SECTION_TITLE } from "./sections/registry";
import s from "./editor.module.css";

export type InspectorTab = "checks" | "quality" | "sources" | "history";

const DIMS: (keyof RubricScores)[] = ["engagement", "alignment", "misconception_coverage", "specificity", "local_relevance"];
const ORIGIN: Record<string, string> = { generate: "Generated", refine: "Revised with AI", manual: "Edited", copy: "Copied" };

export function Inspector({ open, onOpenChange, tab, onTab, lesson, validation, documentId, dirty, onJump, onHistoryMoved }: {
  open: boolean; onOpenChange: (o: boolean) => void; tab: InspectorTab; onTab: (t: InspectorTab) => void;
  lesson: Lesson; validation: LiveValidation; documentId: string | null; dirty: boolean;
  onJump: (section: string) => void; onHistoryMoved: () => void;
}) {
  const tabs: [InspectorTab, string][] = [["checks", "Checks"], ["quality", "Quality"], ["sources", "Sources"], ...(documentId ? [["history", "History"] as [InspectorTab, string]] : [])];
  return (
    <Sheet open={open} onOpenChange={onOpenChange} title="Lesson details">
      <div className={s.tabs} role="tablist">
        {tabs.map(([k, label]) => (
          <button key={k} type="button" role="tab" aria-selected={tab === k} className={s.tab} onClick={() => onTab(k)}>
            {label}{k === "checks" && validation.issues.length > 0 && <span style={{ color: "var(--brick)" }}>{validation.issues.length}</span>}
          </button>
        ))}
      </div>
      {tab === "checks" && <ChecksTab validation={validation} onJump={(k) => { onOpenChange(false); onJump(k); }} />}
      {tab === "quality" && <QualityTab lesson={lesson} />}
      {tab === "sources" && <SourcesTree query={{ topic: lesson.topic, grade: lesson.curriculum_ref.grade, subject: lesson.curriculum_ref.subject, framework: lesson.framework }} />}
      {tab === "history" && documentId && <HistoryTab documentId={documentId} dirty={dirty} onMoved={onHistoryMoved} />}
    </Sheet>
  );
}

function ChecksTab({ validation, onJump }: { validation: LiveValidation; onJump: (section: string) => void }) {
  if (validation.valid === null) return <Row><Spinner /> Checking the lesson…</Row>;
  return (
    <Stack gap={12}>
      {validation.valid
        ? <Banner tone="success" title="Passes every guardrail">Every objective is taught and checked, the hook opens with curiosity, and every question is complete. Ready to export.</Banner>
        : <Banner tone="danger" title={`${validation.issues.length} thing${validation.issues.length === 1 ? "" : "s"} to fix before export`}>Exports compile from this lesson, so it must pass the guardrails first.</Banner>}
      <div>
        {validation.issues.map((i, k) => (
          <div key={k} className={s.checkRow}>
            <Icon.AlertTriangle size={15} style={{ color: "var(--brick)", marginTop: 3 }} />
            <div>
              <div>{i.message}</div>
              <button type="button" onClick={() => onJump(i.section)}>Go to {SECTION_TITLE[i.section] ?? i.section} →</button>
            </div>
          </div>
        ))}
      </div>
      {validation.checking && <Muted>Re-checking your latest edits…</Muted>}
    </Stack>
  );
}

const Row = ({ children }: { children: React.ReactNode }) => <div style={{ display: "flex", gap: 8, alignItems: "center", color: "var(--ink-2)" }}>{children}</div>;

function QualityTab({ lesson }: { lesson: Lesson }) {
  const critique = useCritique();
  const [fresh, setFresh] = useState<Critique | null>(null);
  const scores: Partial<RubricScores> = fresh?.scores ?? lesson.quality;
  const q = lesson.quality;
  return (
    <Stack gap={16}>
      <Muted>{fresh ? "Fresh scores for the current draft." : "Scores from when this version was generated or revised."} Below 0.60 is flagged.</Muted>
      <Stack gap={12}>{DIMS.map((d) => <Meter key={d} label={humanize(d)} value={Number(scores[d] ?? 0)} />)}</Stack>
      <Button size="sm" busy={critique.isPending} icon={<Icon.Gauge size={15} />}
        onClick={() => critique.mutate(lesson, { onSuccess: setFresh })}>Re-score this draft</Button>
      {critique.error && <Banner tone="danger">{describeError(critique.error)}</Banner>}
      {(fresh?.notes || q.notes) && <div><div className={s.label}>Reviewer notes</div><p style={{ fontSize: "var(--text-sm)" }}>{fresh?.notes || q.notes}</p></div>}
      {fresh?.suggestions && Object.keys(fresh.suggestions).length > 0 && (
        <div><div className={s.label}>Suggestions</div>
          <ul style={{ margin: 0, paddingLeft: 18, fontSize: "var(--text-sm)" }}>
            {Object.entries(fresh.suggestions).map(([k, v]) => <li key={k}><b>{SECTION_TITLE[k] ?? humanize(k)}:</b> {v}</li>)}
          </ul>
        </div>
      )}
      {!!q.adjustments?.length && (
        <div><div className={s.label}>Auto-adjusted from the model's draft</div>
          <ul style={{ margin: 0, paddingLeft: 18, fontSize: "var(--text-sm)", color: "var(--ink-2)" }}>{q.adjustments.map((a, i) => <li key={i}>{a}</li>)}</ul>
        </div>
      )}
      {!!q.grounding_sources?.length && (
        <div><div className={s.label}>Grounded in</div><p style={{ fontSize: "var(--text-sm)", color: "var(--ink-2)" }}>{q.grounding_sources.join(" · ")}</p></div>
      )}
    </Stack>
  );
}

function HistoryTab({ documentId, dirty, onMoved }: { documentId: string; dirty: boolean; onMoved: () => void }) {
  const versions = useVersions(documentId);
  const move = useMoveHead(documentId);
  const [head, setHead] = useState<string | null>(null);
  if (versions.isPending) return <SkeletonLines lines={5} />;
  if (versions.error) return <ErrorState error={describeError(versions.error)} onRetry={() => versions.refetch()} />;
  const items = [...versions.data].reverse();
  const go = (dir: "undo" | "redo") => move.mutate(dir, {
    onSuccess: (doc) => { setHead(doc.head_version_id ?? null); void versions.refetch(); onMoved(); },
  });
  const current = head ?? items[0]?.id;
  return (
    <Stack gap={12}>
      <Muted>Every save makes a version. Step the lesson back or forward through them.</Muted>
      <div style={{ display: "flex", gap: 8 }}>
        <Button size="sm" icon={<Icon.Undo2 size={15} />} disabled={dirty} busy={move.isPending && move.variables === "undo"} onClick={() => go("undo")}>Previous version</Button>
        <Button size="sm" icon={<Icon.Redo2 size={15} />} disabled={dirty} busy={move.isPending && move.variables === "redo"} onClick={() => go("redo")}>Next version</Button>
      </div>
      {dirty && <Banner tone="warning">Save or undo your unsaved changes before moving through history.</Banner>}
      {move.error && <Banner tone="danger">{describeError(move.error)}</Banner>}
      <div>
        {items.map((v) => (
          <div key={v.id} className={s.version} data-head={v.id === current || undefined}>
            <span className={s.versionDot} aria-hidden />
            <div>
              <div style={{ fontWeight: 560 }}>{ORIGIN[v.origin] ?? v.origin}{v.id === current && <span className={s.versionMeta}> · current</span>}</div>
              {v.instruction && <div style={{ fontSize: "var(--text-sm)", color: "var(--ink-2)" }}>“{v.instruction}”</div>}
              <div className={s.versionMeta}>{dateTime(v.created_at)}</div>
            </div>
          </div>
        ))}
      </div>
    </Stack>
  );
}
