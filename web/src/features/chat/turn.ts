import type { ArtifactSummary, Citation, LearnFeedback, LearnState, LearnWidget, TurnEvent } from "./types";

/** The assistant's reply as it streams in. */
export interface Turn {
  content: string;
  citations: Citation[];
  /** Progress line while a slow skill works. */
  status: string | null;
  /** A skill (not plain Q&A) handled the turn. */
  skill: string | null;
  artifacts: ArtifactSummary[];
  /** The skill needs one more answer; the next message goes back to it. */
  awaiting: string | null;
  error: string | null;
  title: string | null;
  done: boolean;
  /** Learn mode: the session's progress after this turn. */
  learn: LearnState | null;
  /** Learn mode: the inline check shown under this reply. */
  widget: LearnWidget | null;
  /** Learn mode: the grading of the widget the student just answered. */
  feedback: LearnFeedback | null;
  /** The router offers a lesson on this topic (the student decides). */
  offer: string | null;
}

export const emptyTurn = (): Turn => ({
  content: "", citations: [], status: null, skill: null, artifacts: [], awaiting: null, error: null, title: null, done: false,
  learn: null, widget: null, feedback: null, offer: null,
});

/** Fold one SSE event into the turn. Pure — the whole streaming protocol lives here. */
export function applyEvent(t: Turn, ev: { event: string; data: unknown }): Turn {
  const e = ev as TurnEvent;
  switch (e.event) {
    case "token": return { ...t, content: t.content + String(e.data ?? ""), status: null };
    case "sources": return { ...t, citations: Array.isArray(e.data) ? e.data : [] };
    case "intent": return e.data?.skill && e.data.skill !== "qa" ? { ...t, skill: e.data.skill } : t;
    case "status": return { ...t, status: typeof e.data === "string" ? e.data : e.data?.message ?? null };
    case "artifact": return { ...t, artifacts: [...t.artifacts.filter((a) => a.id !== e.data.id), e.data], status: null };
    case "awaiting": return { ...t, awaiting: e.data?.slot ?? "more detail" };
    case "done": return { ...t, done: true, status: null, title: e.data?.title ?? null };
    case "error": return { ...t, done: true, status: null, error: e.data?.message || "Something went wrong." };
    case "learn_state": return { ...t, learn: e.data, skill: t.skill ?? "learn" };
    case "learn_widget": return { ...t, widget: e.data };
    case "learn_feedback": return { ...t, feedback: e.data };
    case "learn_offer": return { ...t, offer: e.data?.topic ?? null };
    default: return t;
  }
}
