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

/** SSE events of a turn — unknown types are ignored (additive contract). */
export type TurnEvent =
  | { event: "token"; data: string }
  | { event: "sources"; data: Citation[] }
  | { event: "intent"; data: { skill?: string; confidence?: number } }
  | { event: "status"; data: { message?: string } | string }
  | { event: "artifact"; data: ArtifactSummary }
  | { event: "awaiting"; data: { skill?: string; slot?: string } }
  | { event: "done"; data: { title?: string } & Record<string, unknown> }
  | { event: "error"; data: { code?: string; message?: string } };
