import type { Draft } from "immer";
import type { ReactNode } from "react";
import type { Objective, Phase } from "@/lib/api";
import { plural } from "@/lib/format";
import { Icon, IconButton } from "@/ui";
import type { Issue } from "../model/issues";
import type { Lesson } from "../model/ldd";
import { totalMinutes } from "../model/ldd";
import s from "./editor.module.css";

export type Edit = (recipe: (d: Draft<Lesson>) => void, key?: string) => void;

export interface SectionProps {
  lesson: Lesson;
  edit: Edit;
  readOnly: boolean;
  /** Guardrail issues for this section only. */
  issues: Issue[];
}

export function Issues({ issues }: { issues: Issue[] }) {
  if (!issues.length) return null;
  return (
    <div className={s.issues}>
      {issues.map((i, k) => <p key={k} className={s.issue}><Icon.AlertTriangle size={14} aria-hidden /> {i.message}</p>)}
    </div>
  );
}

export function Label({ children }: { children: ReactNode }) {
  return <div className={s.label}>{children}</div>;
}

/** Card for one list item with reorder/remove tools. */
export function ItemCard({ no, title, invalid, onRemove, onMove, readOnly, children, removeLabel }: {
  no?: ReactNode; title?: ReactNode; invalid?: boolean; readOnly?: boolean; children: ReactNode; removeLabel: string;
  onRemove?: () => void; onMove?: (dir: -1 | 1) => void;
}) {
  return (
    <div className={s.item} data-invalid={invalid || undefined}>
      <div className={s.itemHead}>
        {no != null && <span className={s.itemNo}>{no}</span>}
        {title && <div className={s.itemTitle}>{title}</div>}
        {!readOnly && (
          <div className={s.itemTools}>
            {onMove && <>
              <IconButton size="sm" label="Move up" onClick={() => onMove(-1)}><Icon.ArrowUp size={15} /></IconButton>
              <IconButton size="sm" label="Move down" onClick={() => onMove(1)}><Icon.ArrowDown size={15} /></IconButton>
            </>}
            {onRemove && <IconButton size="sm" label={removeLabel} onClick={onRemove}><Icon.Trash2 size={15} /></IconButton>}
          </div>
        )}
      </div>
      {children}
    </div>
  );
}

/** Move item i by dir within an array draft (no-op at the ends). */
export function move<T>(arr: T[], i: number, dir: -1 | 1) {
  const j = i + dir;
  if (j < 0 || j >= arr.length) return;
  [arr[i], arr[j]] = [arr[j]!, arr[i]!];
}

/**
 * Which objectives an item teaches/assesses — toggle chips of the lesson's own
 * objectives, so a link can never point at an objective that doesn't exist.
 */
export function ObjectiveLinks({ objectives, value, onChange, readOnly }: {
  objectives: Objective[]; value: string[]; onChange: (ids: string[]) => void; readOnly?: boolean;
}) {
  const known = new Set(objectives.map((o) => o.id));
  const stale = value.filter((id) => !known.has(id));
  if (!objectives.length) return <span className={s.linksEmpty}>Add an objective first.</span>;
  return (
    <div className={s.links} role="group" aria-label="Objectives">
      {objectives.map((o) => {
        const on = value.includes(o.id);
        return (
          <button key={o.id} type="button" className={s.link} aria-pressed={on} disabled={readOnly} title={o.statement}
            onClick={() => onChange(on ? value.filter((v) => v !== o.id) : [...value, o.id])}>
            {on && <Icon.Check size={12} aria-hidden />}{o.id || "—"}
          </button>
        );
      })}
      {stale.map((id) => (
        <button key={id} type="button" className={s.link} aria-pressed style={{ color: "var(--brick)", background: "var(--brick-tint)" }}
          disabled={readOnly} title="This objective no longer exists — click to unlink" onClick={() => onChange(value.filter((v) => v !== id))}>
          {id} <Icon.X size={11} />
        </button>
      ))}
    </div>
  );
}

const PHASE_COLORS = ["var(--phase-1)", "var(--phase-2)", "var(--phase-3)", "var(--phase-4)", "var(--phase-5)", "var(--phase-6)"];
export const phaseColor = (i: number) => PHASE_COLORS[i % PHASE_COLORS.length]!;

/**
 * The phase timeline — each phase's share of the period, against the lesson's
 * duration. Over-run is flagged in brick, slack in marigold. Used by the editor,
 * unit days and the library.
 */
export function PhaseTimeline({ phases, duration, legend = true }: { phases: Pick<Phase, "name_en" | "minutes">[]; duration: number; legend?: boolean }) {
  const used = totalMinutes(phases as Phase[]);
  const scale = Math.max(used, duration) || 1;
  const diff = used - duration;
  return (
    <div className={s.timeline}>
      <div className={`${s.track} ${diff > 0 ? s.over : ""}`} role="img"
        aria-label={`${plural(phases.length, "phase")}, ${used} of ${duration} minutes`}>
        {phases.map((p, i) => (
          <div key={i} className={s.seg} title={`${p.name_en || `Phase ${i + 1}`} · ${p.minutes} min`}
            style={{ flexGrow: (Number(p.minutes) || 0) / scale, background: phaseColor(i) }} />
        ))}
        {diff < 0 && <div style={{ flexGrow: -diff / scale }} />}
      </div>
      <div className={s.tlMeta}>
        {legend ? (
          <div className={s.tlLegend}>
            {phases.map((p, i) => <span key={i}><i className={s.swatch} style={{ background: phaseColor(i) }} />{p.name_en || `Phase ${i + 1}`} · {p.minutes}′</span>)}
          </div>
        ) : <span />}
        <span className={diff > 0 ? s.tlBad : diff < 0 ? s.tlWarn : undefined} style={{ whiteSpace: "nowrap" }}>
          {used}/{duration} min{diff > 0 ? ` · ${diff} over` : diff < 0 ? ` · ${-diff} spare` : ""}
        </span>
      </div>
    </div>
  );
}
