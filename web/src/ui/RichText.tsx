import { clsx } from "clsx";
import { memo, useEffect, useState, type MouseEvent } from "react";
import s from "./RichText.module.css";

type Render = (src: string, opts?: { citations?: boolean }) => string;
let loaded: Render | null = null;
let loading: Promise<Render> | null = null;

/** Markdown + math + code is ~300 kB — fetched on first use, then shared. */
function loadRenderer(): Promise<Render> {
  loading ??= import("@/lib/markdown/render").then((m) => (loaded = m.renderRich));
  return loading;
}

function useRenderer(): Render | null {
  const [r, setR] = useState<Render | null>(() => loaded);
  useEffect(() => { if (!r) void loadRenderer().then((fn) => setR(() => fn)); }, [r]);
  return r;
}

/**
 * Model output rendered safely (sanitized HTML). Until the renderer arrives, the
 * plain text shows — so streaming never waits on a download. Citation markers
 * `[n]` become buttons that call `onCite(n)`.
 */
export const RichText = memo(function RichText({ text, onCite, className }: { text: string; onCite?: (n: number) => void; className?: string }) {
  const render = useRenderer();
  const onClick = (e: MouseEvent<HTMLDivElement>) => {
    const el = (e.target as HTMLElement).closest<HTMLElement>(".cite");
    if (el && onCite) { e.preventDefault(); onCite(Number(el.dataset.n)); }
  };
  if (!render) return <div className={clsx(s.prose, s.plain, className)}>{text}</div>;
  // safe: renderRich sanitizes with DOMPurify
  return <div className={clsx(s.prose, className)} onClick={onClick} dangerouslySetInnerHTML={{ __html: render(text, { citations: !!onCite }) }} />;
});
