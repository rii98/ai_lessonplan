import type {
  BloomLevel, Differentiation, Hook, LessonDesignDocument, Misconception, Objective, Phase, Question, QuestionType,
} from "@/lib/api";

/**
 * The editor works on a NORMALIZED lesson: every optional list present, so
 * sections never null-check. The server fills these defaults too, so normalizing
 * never changes meaning — and what we send back is still a valid LDD.
 */
export type Lesson = Omit<Required<LessonDesignDocument>, "homework" | "differentiation" | "formative_checks"> & {
  homework: LessonDesignDocument["homework"] | null;
  differentiation: Required<Differentiation>;
  formative_checks: NormalizedQuestion[];
};
export type NormalizedQuestion = Question & { key_points: string[] };

export const BLOOM: BloomLevel[] = ["remember", "understand", "apply", "analyze", "evaluate", "create"];
export const HOOK_KINDS: Hook["kind"][] = ["scenario", "question", "prediction", "demonstration"];
export const DURATIONS = [30, 45, 60] as const;
export const LANGUAGES = [
  { value: "en-ne", label: "English + Nepali" },
  { value: "en", label: "English" },
  { value: "ne", label: "नेपाली" },
] as const;
export const FRAMEWORKS = [
  { value: "5E", label: "5E" },
  { value: "gradual_release", label: "Gradual release" },
  { value: "inquiry", label: "Inquiry" },
] as const;

export function normalizeQuestion(q: Question): NormalizedQuestion {
  return { ...q, answer: q.answer ?? "", explanation: q.explanation ?? "", key_points: q.key_points ?? [], objective_ids: q.objective_ids ?? [] };
}

export function normalizeLesson(l: LessonDesignDocument): Lesson {
  return {
    ...l,
    language: l.language ?? "en-ne",
    framework: l.framework ?? "5E",
    prior_knowledge: l.prior_knowledge ?? [],
    misconceptions: l.misconceptions ?? [],
    local_context: l.local_context ?? [],
    materials: l.materials ?? [],
    differentiation: { struggling: [], on_level: [], advanced: [], ...l.differentiation },
    formative_checks: (l.formative_checks ?? []).map(normalizeQuestion),
    homework: l.homework ?? null,
    quality: { engagement: 0, alignment: 0, misconception_coverage: 0, specificity: 0, local_relevance: 0, notes: "", grounding_sources: [], adjustments: [], ...l.quality },
  };
}

/** The next free id with a prefix: O1, O2… / Q1, Q2… */
export function nextId(prefix: string, taken: string[]): string {
  const used = new Set(taken);
  for (let n = 1; ; n++) if (!used.has(`${prefix}${n}`)) return `${prefix}${n}`;
}

export const blankObjective = (taken: string[]): Objective => ({ id: nextId("O", taken), statement: "", bloom: "understand" });
export const blankMisconception = (): Misconception => ({ statement: "", correction: "", source: null });
export const blankPhase = (): Phase => ({ name_en: "", name_ne: "", teacher_activities: [""], student_activities: [""], minutes: 5, objective_ids: [] });
export const blankQuestion = (taken: string[], type: QuestionType = "short_answer", objective_ids: string[] = []): NormalizedQuestion =>
  ({ id: nextId("Q", taken), type, prompt: "", answer: "", explanation: "", objective_ids, options: null, pairs: null, key_points: [] });

export const totalMinutes = (phases: Phase[]) => phases.reduce((s, p) => s + (Number(p.minutes) || 0), 0);
