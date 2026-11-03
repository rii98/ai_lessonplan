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

/**
 * What a user turn *is*: typed speech, a tapped action (Hint, I know this…) or an
 * answer to an inline check. Only speech gets a bubble — the other two are
 * receipts, so the student's own words stand out from button presses.
 */
export type UserKind = "speech" | "action" | "answer";

export function userKind(meta: Record<string, unknown> | undefined): UserKind {
  if (meta?.widget_response) return "answer";
  if (meta?.action) return "action";
  return "speech";
}

export function UserMessage({ text, kind = "speech" }: { text: string; kind?: UserKind }) {
  if (kind === "speech") return <div className={`${s.msg} ${s.user}`}>{text}</div>;
  const answer = kind === "answer";
  return (
    <div className={s.receipt} data-kind={kind}>
      {answer ? <Icon.CornerDownRight size={13} aria-hidden /> : <Icon.MousePointerClick size={13} aria-hidden />}
      {answer && <span className={s.receiptLabel}>Your answer</span>}
      <span className={s.receiptText}>{answer ? text.replace(/^Answered:\s*/, "") : text}</span>
    </div>
  );
}

export function ArtifactCard({ a, onOpen }: { a: ArtifactSummary; onOpen: () => void }) {
  const best = a.best != null ? (Number.isInteger(a.best) ? a.best : a.best.toFixed(1)) : null;
  const status = a.in_progress ? "In progress"
    : !a.attempts ? "Not tried yet"
    : `${a.attempts} attempt${a.attempts > 1 ? "s" : ""}${best != null ? ` · best ${best}/${a.question_count}` : ""}`;
  const cta = a.in_progress ? "Resume" : a.attempts ? "Play again" : "Start quiz";
  const pct = a.best != null && a.question_count ? Math.round((a.best / a.question_count) * 100) : null;
  return (
    <button type="button" className={s.card} onClick={onOpen}>
      <span className={s.cardIcon}><Icon.ListChecks size={22} /></span>
      <span className={s.cardBody}>
        <span className={s.cardKicker}>Quiz · {a.question_count} questions</span>
        <span className={s.cardTitle}>{a.title}</span>
        <span className={s.cardMeta}>{status}</span>
        {pct != null && <span className={s.cardBar} aria-hidden><i style={{ width: `${pct}%` }} /></span>}
      </span>
      <span className={s.cardCta}>{a.in_progress || a.attempts ? <Icon.RotateCcw size={15} /> : <Icon.Play size={15} />}{cta}</span>
    </button>
  );
}

const SKILL_NAMES: Record<string, string> = { learn: "Lesson", quiz: "Quiz" };

export function AssistantMessage({ content, citations = [], artifacts = [], status, pending, skill, showName = true, onCite, onOpenArtifact, onQuickAction, onTeach, children }: {
  content: string; citations?: Citation[]; artifacts?: ArtifactSummary[]; status?: string | null; pending?: boolean; skill?: string | null;
  /** Name the speaker only where the mode changes — a label on every turn is noise. */
  showName?: boolean;
  onCite: (c: Citation) => void; onOpenArtifact: (id: string) => void; onQuickAction?: (text: string) => void;
  /** Offer a scaffolded lesson on what was just asked (learn mode). */
  onTeach?: () => void;
  /** Learn-mode extras under the reply: the inline widget, quick chips, a lesson offer. */
  children?: ReactNode;
}) {
  const byN = new Map(citations.map((c) => [c.n, c]));
  return (
    <div className={`${s.msg} ${s.ai}`} data-live={pending || undefined}>
      {showName && (
        <div className={s.aiName}>
          {skill === "learn" ? <Icon.GraduationCap size={14} aria-hidden /> : <Icon.BookOpen size={14} aria-hidden />}
          {skill && skill !== "qa" ? SKILL_NAMES[skill] ?? skill : "LessonForge"}
        </div>
      )}
      {content ? <div className={s.voice}><RichText text={content} onCite={(n) => { const c = byN.get(n); if (c) onCite(c); }} /></div>
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
