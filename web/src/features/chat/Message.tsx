import type { ReactNode } from "react";
import { breadcrumb } from "@/lib/format";
import { RichText, Icon } from "@/ui";
import type { ArtifactSummary, Citation } from "./types";
import s from "./chat.module.css";

/** A human label for a source: its heading trail, else its source name, else the collection. */
export function citationLabel(c: Citation): string {
  const trail = breadcrumb(String(c.metadata?.heading_path ?? c.metadata?.chapter ?? ""));
  const looksLikeId = !!c.source && !/\s/.test(c.source) && /[:/_-]\d/.test(c.source);
  return trail || (!looksLikeId && c.source) || (c.collection ? c.collection.replace("_", " ") : "Source");
}

export function UserMessage({ text }: { text: string }) {
  return <div className={`${s.msg} ${s.user}`}>{text}</div>;
}

export function ArtifactCard({ a, onOpen }: { a: ArtifactSummary; onOpen: () => void }) {
  const status = a.in_progress ? "In progress"
    : !a.attempts ? `${a.question_count} questions · not tried yet`
    : `${a.question_count} questions · ${a.attempts} attempt${a.attempts > 1 ? "s" : ""}${a.best != null ? ` · best ${Number.isInteger(a.best) ? a.best : a.best.toFixed(1)}/${a.question_count}` : ""}`;
  return (
    <button type="button" className={s.card} onClick={onOpen}>
      <span className={s.cardIcon}><Icon.ListChecks size={18} /></span>
      <span style={{ minWidth: 0 }}><div style={{ fontWeight: 600 }}>{a.title}</div><div className={s.cardMeta}>{status}</div></span>
      <Icon.ChevronRight size={18} style={{ color: "var(--ink-3)" }} />
    </button>
  );
}

export function AssistantMessage({ content, citations = [], artifacts = [], status, pending, skill, onCite, onOpenArtifact, onQuickAction, onTeach, children }: {
  content: string; citations?: Citation[]; artifacts?: ArtifactSummary[]; status?: string | null; pending?: boolean; skill?: string | null;
  onCite: (c: Citation) => void; onOpenArtifact: (id: string) => void; onQuickAction?: (text: string) => void;
  /** Offer a scaffolded lesson on what was just asked (learn mode). */
  onTeach?: () => void;
  /** Learn-mode extras under the reply: the inline widget, quick chips, a lesson offer. */
  children?: ReactNode;
}) {
  const byN = new Map(citations.map((c) => [c.n, c]));
  return (
    <div className={`${s.msg} ${s.ai}`}>
      <div className={s.aiName}>LessonForge{skill && skill !== "qa" && <span style={{ color: "var(--marigold-ink)" }}>· {skill === "learn" ? "lesson" : skill}</span>}</div>
      {content ? <RichText text={content} onCite={(n) => { const c = byN.get(n); if (c) onCite(c); }} />
        : pending && <div className={s.status}><span className={s.typing} aria-label="Thinking"><i /><i /><i /></span>{status}</div>}
      {content && pending && status && <div className={s.status}>{status}</div>}
      {citations.length > 0 && (
        <div className={s.refs} aria-label={`${citations.length} sources`}>
          {citations.map((c) => (
            <button key={c.n} type="button" className={s.ref} onClick={() => onCite(c)} title={c.source}>
              <span className={s.refN}>{c.n}</span><span className={s.refText}>{citationLabel(c)}</span>
            </button>
          ))}
        </div>
      )}
      {artifacts.map((a) => <ArtifactCard key={a.id} a={a} onOpen={() => onOpenArtifact(a.id)} />)}
      {children}
      {!pending && content && !artifacts.length && (!skill || skill === "qa") && onQuickAction && (
        <div className={s.quick}>
          {onTeach && <button type="button" onClick={onTeach}>Teach me this step by step</button>}
          <button type="button" onClick={() => onQuickAction("Quiz me on this")}>Quiz me on this</button>
        </div>
      )}
    </div>
  );
}
