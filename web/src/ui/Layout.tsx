import { clsx } from "clsx";
import type { CSSProperties, ElementType, ReactNode } from "react";
import s from "./Layout.module.css";

export function Page({ children, width = "default", className }: { children: ReactNode; width?: "narrow" | "default" | "wide"; className?: string }) {
  return <div className={clsx(s.page, width !== "default" && s[width], className)}>{children}</div>;
}

export function PageHeader({ eyebrow, title, lede, actions }: { eyebrow?: ReactNode; title: ReactNode; lede?: ReactNode; actions?: ReactNode }) {
  return (
    <header className={s.header}>
      <div style={{ minWidth: 0 }}>
        {eyebrow && <div className={s.eyebrow}>{eyebrow}</div>}
        <h1 className={s.title}>{title}</h1>
        {lede && <p className={s.lede}>{lede}</p>}
      </div>
      {actions && <div className={s.actions}>{actions}</div>}
    </header>
  );
}

export function Section({ title, hint, actions, children, id, className }: {
  title?: ReactNode; hint?: ReactNode; actions?: ReactNode; children: ReactNode; id?: string; className?: string;
}) {
  return (
    <section className={clsx(s.section, className)} id={id} aria-labelledby={id && title ? `${id}-h` : undefined}>
      {(title || actions) && (
        <div className={s.sectionHead}>
          <div>
            {title && <h2 id={id ? `${id}-h` : undefined} className={s.sectionTitle}>{title}</h2>}
            {hint && <p className={s.sectionHint}>{hint}</p>}
          </div>
          {actions && <div className={s.actions}>{actions}</div>}
        </div>
      )}
      {children}
    </section>
  );
}

export const Panel = ({ children, className, as: As = "div", style }: { children: ReactNode; className?: string; as?: ElementType; style?: CSSProperties }) =>
  <As className={clsx(s.panel, className)} style={style}>{children}</As>;

export function Stack({ gap = 12, children, className, style }: { gap?: number; children: ReactNode; className?: string; style?: CSSProperties }) {
  return <div className={clsx(s.stack, className)} style={{ gap, ...style }}>{children}</div>;
}

export function Row({ gap = 8, children, className, justify, style }: { gap?: number; children: ReactNode; className?: string; justify?: CSSProperties["justifyContent"]; style?: CSSProperties }) {
  return <div className={clsx(s.row, className)} style={{ gap, justifyContent: justify, ...style }}>{children}</div>;
}

export const Muted = ({ children, as: As = "p" }: { children: ReactNode; as?: ElementType }) => <As className={s.muted}>{children}</As>;
