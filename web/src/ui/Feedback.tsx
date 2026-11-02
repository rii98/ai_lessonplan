import { clsx } from "clsx";
import type { CSSProperties, ReactNode } from "react";
import { AlertTriangle, CheckCircle2, Info, XCircle } from "./icons";
import s from "./Feedback.module.css";

export type Tone = "info" | "success" | "warning" | "danger";
const ICON = { info: Info, success: CheckCircle2, warning: AlertTriangle, danger: XCircle };

export function Banner({ tone = "info", title, children, actions, className }: {
  tone?: Tone; title?: ReactNode; children?: ReactNode; actions?: ReactNode; className?: string;
}) {
  const Icon = ICON[tone];
  return (
    <div className={clsx(s.banner, s[tone], className)} role={tone === "danger" ? "alert" : "status"}>
      <Icon size={16} aria-hidden />
      <div className={s.bannerBody}>
        {title && <div className={s.bannerTitle}>{title}</div>}
        {children}
      </div>
      {actions && <div className={s.bannerActions}>{actions}</div>}
    </div>
  );
}

export const Spinner = ({ label = "Loading" }: { label?: string }) =>
  <span className={s.spinner} role="status" aria-label={label} />;

export const Skeleton = ({ w = "100%", h = 14, style }: { w?: number | string; h?: number | string; style?: CSSProperties }) =>
  <span className={s.skeleton} style={{ width: w, height: h, ...style }} aria-hidden />;

/** A few skeleton lines — for lists and documents while they load. */
export function SkeletonLines({ lines = 3, gap = 10 }: { lines?: number; gap?: number }) {
  return (
    <div style={{ display: "grid", gap }} aria-busy="true" aria-label="Loading">
      {Array.from({ length: lines }, (_, i) => <Skeleton key={i} w={`${92 - ((i * 17) % 35)}%`} />)}
    </div>
  );
}

export function EmptyState({ icon, title, children, action }: { icon?: ReactNode; title: ReactNode; children?: ReactNode; action?: ReactNode }) {
  return (
    <div className={s.empty}>
      {icon && <div className={s.emptyIcon} aria-hidden>{icon}</div>}
      <h3>{title}</h3>
      {children && <p>{children}</p>}
      {action}
    </div>
  );
}

export interface Stage { label: string; detail: string }

/**
 * Honest progress for slow AI work: what is happening and how long it has taken.
 * The server doesn't report which stage it is in, so we never pretend to know —
 * no fake percentages.
 */
export function ProgressPanel({ title, elapsed, stages, note }: { title: ReactNode; elapsed: number; stages?: Stage[]; note?: ReactNode }) {
  const mm = Math.floor(elapsed / 60), ss = String(elapsed % 60).padStart(2, "0");
  return (
    <div className={s.progress} role="status" aria-live="polite">
      <div className={s.progressHead}><Spinner label="Working" /> {title}<span className={s.elapsed}>{mm}:{ss}</span></div>
      {stages && (
        <ol className={s.stages}>
          {stages.map((st) => <li key={st.label}><span><b>{st.label}</b> — {st.detail}</span></li>)}
        </ol>
      )}
      {note && <p className={s.progressNote}>{note}</p>}
    </div>
  );
}

export function ErrorState({ error, onRetry, title = "Couldn't load this" }: { error: ReactNode; onRetry?: () => void; title?: ReactNode }) {
  return (
    <Banner tone="danger" title={title}
      actions={onRetry && <button type="button" onClick={onRetry} style={{ border: "1px solid currentColor", background: "none", borderRadius: 6, padding: "4px 10px", cursor: "pointer", color: "inherit" }}>Try again</button>}>
      {error}
    </Banner>
  );
}
