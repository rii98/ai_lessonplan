/**
 * Friendly names for the generated contract types (`schema.d.ts`, produced from
 * docs/openapi.v1.json by `npm run gen:api`). Never hand-edit shapes here — when
 * the server changes a model, regenerate.
 */
import type { components } from "./schema";

type S = components["schemas"];

/**
 * Response models: FastAPI serializes every field, including those with server
 * defaults, so a response has them all even though the schema marks defaulted
 * fields optional (they are optional in REQUESTS).
 */
type Res<T> = { [K in keyof T]-?: T[K] };

export type LessonDesignDocument = S["LessonDesignDocument"];
export type CurriculumRef = S["CurriculumRef"];
export type Objective = S["Objective"];
export type BloomLevel = S["BloomLevel"];
export type Hook = S["Hook"];
export type Phase = S["Phase"];
export type Question = S["Question"];
export type QuestionType = S["QuestionType"];
export type Difficulty = S["Difficulty"];
export type MatchPair = S["MatchPair"];
export type Misconception = S["Misconception"];
export type Differentiation = S["Differentiation"];
export type Homework = S["Homework"];
export type QualityReport = S["QualityReport"];
export type RubricScores = S["RubricScores"];
export type Critique = Res<S["Critique"]>;
export type RefineResult = Omit<S["RefineResult"], "diff"> & { diff?: Record<string, { before?: unknown; after?: unknown }> };
export type ValidationResult = { valid: boolean; errors: { loc: string; msg: string }[] };

export type GenerateRequest = S["GenerateRequest"];
export type SaveLessonRequest = S["SaveLessonRequest"];
export type SavedLesson = { document: StoredDocument; version_id: string; ldd: LessonDesignDocument };
export type StoredDocument = Res<S["StoredDocument"]>;
export type DocumentKind = S["DocumentKind"];
export type DocumentView = { document: StoredDocument; ldd: LessonDesignDocument };
export type DocumentVersionInfo = Res<S["DocumentVersionInfo"]>;
export type VersionOrigin = S["VersionOrigin"];
export type Visibility = S["Visibility"];
export type CopiedDocument = { document: StoredDocument; version_id: string };

export type UnitRequest = S["UnitRequest"];
export type GenerateUnitRequest = S["GenerateUnitRequest"];
export type UnitPlan = S["UnitPlan"];
export type DayPlan = S["DayPlan"];
export type PlanResponse = S["PlanResponse"];
export type CoverageReport = S["CoverageReport"];
export type CoherenceReport = S["CoherenceReport"];
export type CoherenceIssue = S["CoherenceIssue"];
export type UnitDesignDocument = S["UnitDesignDocument"];
export type SavedUnit = { document: StoredDocument; version_id: string; unit: UnitDesignDocument };
export type UnitView = { document: StoredDocument; unit: UnitDesignDocument };

export type AssessmentSpec = S["AssessmentSpec"];
export type TypeSpec = S["TypeSpec"];
export type ArtifactKind = S["ArtifactKind"];

export type TeacherProfile = S["TeacherProfile"];

export type Conversation = Res<S["Conversation"]>;
export type ChatDefaults = S["ChatDefaults"];
export type FilterMode = S["FilterMode"];
export type Collection = S["Collection"];

export interface Page<T> {
  items: T[];
  next_cursor?: string | null;
}
