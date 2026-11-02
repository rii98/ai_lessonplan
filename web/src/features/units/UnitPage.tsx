import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { PhaseTimeline } from "@/features/lessons";
import { useMe } from "@/features/session";
import { describeError, type CoherenceReport, type LessonDesignDocument } from "@/lib/api";
import { humanize, plural, relativeTime } from "@/lib/format";
import { Badge, Banner, Button, ConfirmDialog, ErrorState, Icon, Menu, Page, PageHeader, Section, SkeletonLines, useToast } from "@/ui";
import { useExportUnit, useRegenerateDay, useUnit } from "./api";
import s from "./units.module.css";

export function CoherenceBanner({ report, compact }: { report?: CoherenceReport | null; compact?: boolean }) {
  if (!report) return null;
  const issues = report.issues ?? [];
  if (report.ok || !issues.length) return <Banner tone="success" title="The arc holds together">{compact ? null : "No repeated hooks, re-taught topics or broken hand-offs between days."}</Banner>;
  return (
    <Banner tone="warning" title={`${plural(issues.length, "coherence note")} across the days`}>
      <ul style={{ margin: "4px 0 0", paddingLeft: 18 }}>
        {issues.map((i, k) => <li key={k}><b>{humanize(i.kind)}</b>{i.day != null ? ` (day ${i.day})` : ""}{i.detail ? ` — ${i.detail}` : ""}</li>)}
      </ul>
    </Banner>
  );
}

/** `/units/:unitId` — the generated unit as a run of days. */
export function UnitPage() {
  const { unitId = "" } = useParams();
  const navigate = useNavigate();
  const toast = useToast();
  const me = useMe().data;
  const q = useUnit(unitId);
  const regen = useRegenerateDay(unitId);
  const exp = useExportUnit(unitId);
  const [confirmDay, setConfirmDay] = useState<number | null>(null);

  if (q.isPending) return <Page><SkeletonLines lines={8} /></Page>;
  if (q.error || !q.data) return <Page width="narrow"><ErrorState title="Couldn't open this unit" error={describeError(q.error)} onRetry={() => q.refetch()} /></Page>;
  const { unit, document: doc } = q.data;
  const mine = me?.id === doc.owner_id;
  const ref = unit.curriculum_ref;

  return (
    <Page>
      <PageHeader eyebrow={<Link to="/units"><Icon.ArrowLeft size={13} /> Units</Link>} title={unit.title}
        lede={unit.big_idea || undefined}
        actions={<>
          <Button icon={<Icon.Download size={16} />} busy={exp.isPending}
            onClick={() => exp.mutate(undefined, { onSuccess: (n) => toast.show(`Downloaded ${n}`), onError: (e) => toast.error(describeError(e)) })}>Download all</Button>
          <Menu trigger={<Button variant="primary" icon={<Icon.ListChecks size={16} />}>Unit test</Button>} items={[
            { label: "A unit test (quiz)…", icon: <Icon.ListChecks size={16} />, onSelect: () => navigate(`/studio/quiz?unit=${encodeURIComponent(unitId)}`) },
            { label: "A unit worksheet…", icon: <Icon.ClipboardList size={16} />, onSelect: () => navigate(`/studio/worksheet?unit=${encodeURIComponent(unitId)}`) },
          ]} />
        </>} />

      <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginBottom: 16 }}>
        <Badge tone="pine">Grade {ref.grade} · {ref.subject}</Badge>
        <Badge>{plural(unit.days.length, "day")}</Badge>
        <Badge>Updated {relativeTime(doc.updated_at)}</Badge>
        {!mine && <Badge tone="marigold"><Icon.Lock size={11} /> Shared by {doc.owner_name || "a colleague"}</Badge>}
      </div>

      <div style={{ display: "grid", gap: 12, marginBottom: 8 }}>
        <CoherenceBanner report={unit.coherence} />
        {!!unit.unit_outcomes?.length && (
          <div>
            <div className={s.lbl}>By the end of the unit</div>
            <ul className={s.objList}>{unit.unit_outcomes.map((o, i) => <li key={i}>{o}</li>)}</ul>
          </div>
        )}
      </div>

      <Section title="Day by day">
        <div className={s.timelineDays}>
          {unit.days.map((d, i) => (
            <DayCard key={i} day={d} n={i + 1} unitId={unitId} editable={mine}
              regenerating={regen.isPending && regen.variables === i + 1}
              onRegenerate={() => setConfirmDay(i + 1)} />
          ))}
        </div>
      </Section>

      <ConfirmDialog open={confirmDay != null} onOpenChange={(o) => !o && setConfirmDay(null)} busy={regen.isPending}
        title={`Rewrite day ${confirmDay}?`} confirmLabel="Rewrite the day"
        body="The AI writes this day again from the arc. Your edits to this day are replaced (the old version stays in the unit's history)."
        onConfirm={() => regen.mutate(confirmDay!, {
          onSuccess: () => { toast.show(`Day ${confirmDay} rewritten`); setConfirmDay(null); },
          onError: (e) => { toast.error(describeError(e)); setConfirmDay(null); },
        })} />
    </Page>
  );
}

function DayCard({ day, n, unitId, editable, regenerating, onRegenerate }: {
  day: LessonDesignDocument; n: number; unitId: string; editable: boolean; regenerating: boolean; onRegenerate: () => void;
}) {
  return (
    <article className={s.dayCard} aria-label={`Day ${n}`}>
      <div className={s.dayNo}><span>Day</span><b>{n}</b></div>
      <div className={s.dayBody}>
        <Link to={`/units/${unitId}/days/${n}`} className={s.dayTopic} style={{ color: "var(--ink)", textDecoration: "none" }}>{day.topic}</Link>
        {day.engagement_hook?.prompt && <p className={s.hook}>{day.engagement_hook.prompt}</p>}
        {!!day.objectives?.length && <ul className={s.objList}>{day.objectives.map((o) => <li key={o.id}>{o.statement}</li>)}</ul>}
        <PhaseTimeline phases={day.phases ?? []} duration={day.duration_min} legend={false} />
        <div className={s.dayActions}>
          <Link to={`/units/${unitId}/days/${n}`}><Button size="sm" icon={editable ? <Icon.PenLine size={14} /> : <Icon.FileText size={14} />}>{editable ? "Open & edit" : "Open"}</Button></Link>
          {editable && <Button size="sm" variant="quiet" busy={regenerating} icon={<Icon.RefreshCw size={14} />} onClick={onRegenerate}>Rewrite with AI</Button>}
        </div>
      </div>
    </article>
  );
}
