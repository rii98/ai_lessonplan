import type { ChatDefaults, Conversation, FilterMode } from "@/lib/api";

// ── contract gaps (chat routes return untyped dicts; see docs/CHAT_EVENTS.md) ──

export interface Citation { n: number; source?: string; collection?: string; text?: string; score?: number; metadata?: Record<string, unknown> }

export interface ChatMessage {
  id?: string;
  role: "user" | "assistant";
  content: string;
  citations?: Citation[];
  meta?: { artifact_ids?: string[]; intent?: { skill?: string } | null; [k: string]: unknown };
}

/** An interactive artifact a skill produced (a quiz today). */
export interface ArtifactSummary {
  id: string; kind: string; title: string; message_id?: string | null; created_at?: string;
  question_count: number; attempts: number; best: number | null; last: number | null; in_progress: string | null;
}

export interface ConversationDetail { conversation: Conversation; messages: ChatMessage[]; artifacts: ArtifactSummary[] }

export interface ChatConfig {
  collections: string[]; filter_fields: string[]; default_mode: FilterMode;
  scope_mode: "off" | "prefill" | "locked" | string; scope_defaults: Partial<ChatDefaults>;
}

export interface Scope { mode: FilterMode; grade: number | null; subject: string | null; class: string | null; collections: string[] | null }

// ── learn mode (docs/LEARN_MODE.md; services/learn) — contract gaps ──

export type ObjectiveStatus = "pending" | "active" | "mastered" | "skipped" | "left";

/** The student-safe view of a learning session (LearningSession.public()). */
export interface LearnState {
  id: string; conversation_id: string; status: "planning" | "active" | "paused" | "completed" | "ended";
  goal: string; topic: string; ungrounded: boolean; pace: "slow" | "normal" | "fast"; cursor: string;
  objectives: { id: string; title: string; status: ObjectiveStatus; early: boolean; mastery: number }[];
  mastered: number; total: number; parking_lot: string[]; choices: string[];
}

/** An inline check — the quiz play-view shape plus learn fields. Never carries the answer. */
export interface LearnWidget {
  id: string; number: number; type: string; widget: string; prompt: string; difficulty?: string | null;
  choices?: string[]; left?: string[]; right?: { letter: string; text: string }[]; items?: { letter: string; text: string }[];
  kind: string; label: string; purpose: "check" | "challenge" | "warm_up" | "skip"; objective_id: string; ask_confidence: boolean;
}

export interface LearnFeedback {
  widget_id: string; correct: boolean | null; score: number; expected: string; explanation?: string; feedback?: string; reveal?: string;
}

export type LearnAction = "hint" | "example" | "skip" | "faster" | "slower" | "pause" | "end" | "choose" | "resume";

/** SSE events of a turn — unknown types are ignored (additive contract). */
export type TurnEvent =
  | { event: "token"; data: string }
  | { event: "sources"; data: Citation[] }
  | { event: "intent"; data: { skill?: string; confidence?: number } }
  | { event: "status"; data: { message?: string } | string }
  | { event: "artifact"; data: ArtifactSummary }
  | { event: "awaiting"; data: { skill?: string; slot?: string } }
  | { event: "done"; data: { title?: string } & Record<string, unknown> }
  | { event: "error"; data: { code?: string; message?: string } }
  | { event: "learn_state"; data: LearnState }
  | { event: "learn_widget"; data: LearnWidget }
  | { event: "learn_feedback"; data: LearnFeedback }
  | { event: "learn_offer"; data: { topic: string } };
