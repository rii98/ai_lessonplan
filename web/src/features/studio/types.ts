import type { AssessmentSpec, CurriculumRef, GenerateRequest, Objective, Question, QuestionType, TypeSpec } from "@/lib/api";

// ── contract gaps: the artifact routes return untyped dicts (domain/artifacts.py) ──
export interface Quiz {
  topic: string; curriculum_ref: CurriculumRef; objectives: Objective[]; questions: Question[];
  instructions: string; spec?: AssessmentSpec | null; notes: string[]; grounding_sources: string[];
}
export interface Worksheet {
  topic: string; curriculum_ref: CurriculumRef; objectives: Objective[]; tasks: string[]; questions: Question[];
  spec?: AssessmentSpec | null; notes: string[]; grounding_sources: string[];
}
export interface Slide { heading: string; subtitle?: string | null; bullets: string[] }
export interface Slides { topic: string; curriculum_ref: CurriculumRef; subtitle: string; slides: Slide[]; grounding_sources: string[] }

export type StudioKind = "quiz" | "worksheet" | "slides";
export type Artifact = Quiz | Worksheet | Slides;

/** GET /artifacts/question-types */
export interface QuestionCatalog {
  types: { type: QuestionType; label: string; hint: string; default_marks: number | null; default_count: number; retrieval: string }[];
  difficulties: TypeSpec["difficulty"][];
  scopes: { value: AssessmentSpec["scope"]; label: string; help: string }[];
  presets: { label: string; types: TypeSpec[] }[];
  limits: { max_per_type: number; max_total: number };
}

/** POST /artifacts/retrieval-plan */
export interface RetrievalPlan { passes: { granularity: string; top_n: number; why: string }[]; total_questions: number; total_marks: number }

/** What Home hands to the studio in router state. */
export interface StudioHandoff { brief: GenerateRequest }

/** Where the questions come from. */
export type StudioSource =
  | { type: "brief"; brief: GenerateRequest }
  | { type: "lesson"; documentId: string }
  | { type: "unitDay"; unitId: string; day: number }
  | { type: "unit"; unitId: string };
