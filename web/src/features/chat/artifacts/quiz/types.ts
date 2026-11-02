import type { ArtifactSummary } from "../../types";

// contract gaps — services/chat/quiz_play.py
export interface PlayQuestion {
  id: string; number: number; type: string; widget: "choice" | "boolean" | "text" | "number" | "written" | "match" | "order" | string;
  prompt: string; difficulty?: string | null;
  choices?: string[]; left?: string[]; right?: { letter: string; text: string }[]; items?: { letter: string; text: string }[];
}
export interface PlayView { id: string; kind: string; title: string; topic: string; sources: string[]; notes: string[]; questions: PlayQuestion[] }
export interface AttemptRow { id: string; status: string; score: number; total: number; answered: number; created_at: string }
export interface ArtifactDetail { summary: ArtifactSummary; play: PlayView; attempts: AttemptRow[] }
export interface Graded {
  correct: boolean | null; score: number; expected: string; explanation?: string; key_points?: string[]; feedback?: string;
  self_check?: boolean; pending?: boolean; response?: unknown;
}
export interface Review {
  attempt: { id: string; status: string; score: number; total: number; percent: number; created_at: string };
  by_type: Record<string, { score: number; of: number }>;
  questions: { id: string; number: number; type: string; prompt: string; answered: boolean; response?: unknown; score?: number; expected?: string; explanation?: string }[];
  missed: string[];
}
