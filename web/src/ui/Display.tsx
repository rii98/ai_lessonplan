import { clsx } from "clsx";
import type { ReactNode } from "react";
import s from "./Display.module.css";

export function Badge({ tone = "neutral", outline, children, title }: {
  tone?: "neutral" | "pine" | "marigold" | "brick"; outline?: boolean; children: ReactNode; title?: string;
}) {
  return <span title={title} className={clsx(s.badge, tone !== "neutral" && s[tone], outline && s.outline)}>{children}</span>;
}

export type Status = "ok" | "attention" | "error" | "empty";
const STATUS_LABEL: Record<Status, string> = { ok: "Complete", attention: "Needs attention", error: "Fails a guardrail", empty: "Empty" };

export const StatusDot = ({ status }: { status: Status }) =>
  <span className={clsx(s.dot, s[`dot_${status}`])} role="img" aria-label={STATUS_LABEL[status]} title={STATUS_LABEL[status]} />;

/** A 0..1 score as a labelled bar. Below `low` it turns marigold (needs attention). */
export function Meter({ label, value, low = 0.6 }: { label: string; value: number; low?: number }) {
  const pct = Math.max(0, Math.min(1, value)) * 100;
  return (
    <div className={clsx(s.meter, value < low && s.meterLow)}>
      <span className={s.meterLabel}>{label}</span>
      <span className={s.meterValue}>{value.toFixed(2)}</span>
      <div className={s.meterTrack} role="meter" aria-label={label} aria-valuemin={0} aria-valuemax={1} aria-valuenow={value}>
        <div className={s.meterFill} style={{ width: `${pct}%` }} />
      </div>
    </div>
  );
}

export interface SegmentedOption<T extends string> { value: T; label: ReactNode; disabled?: boolean }

/** A single choice among a few options (a radio group that looks like tabs). */
export function Segmented<T extends string>({ value, onChange, options, label }: {
  value: T; onChange: (v: T) => void; options: SegmentedOption<T>[]; label: string;
}) {
  return (
    <div className={s.seg} role="radiogroup" aria-label={label}
      onKeyDown={(e) => {
        if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
        const live = options.filter((o) => !o.disabled);
        const i = live.findIndex((o) => o.value === value);
        const next = live[(i + (e.key === "ArrowRight" ? 1 : live.length - 1)) % live.length];
        if (next) { e.preventDefault(); onChange(next.value); }
      }}>
      {options.map((o) => (
        <button key={o.value} type="button" role="radio" aria-checked={o.value === value} disabled={o.disabled}
          tabIndex={o.value === value ? 0 : -1} className={s.segItem} onClick={() => onChange(o.value)}>
          {o.label}
        </button>
      ))}
    </div>
  );
}
