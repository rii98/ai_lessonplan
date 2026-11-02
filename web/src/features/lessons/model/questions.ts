import type { QuestionType } from "@/lib/api";

/** Per-type editing rules — what a question of each type needs (mirrors domain/ldd.py). */
export interface QuestionTypeMeta {
  label: string;
  /** Needs `options` (MCQ choices / ordering steps). */
  options?: "choices" | "steps";
  pairs?: boolean;
  /** Answer is computed from the structure; no answer field. */
  derivedAnswer?: boolean;
  /** Written answers carry marking points. */
  keyPoints?: boolean;
  answerHint?: string;
  longAnswer?: boolean;
}

export const QUESTION_TYPES: Record<QuestionType, QuestionTypeMeta> = {
  mcq: { label: "Multiple choice", options: "choices", answerHint: "Copy the correct option exactly" },
  true_false: { label: "True / false", answerHint: "True or False" },
  fill_blank: { label: "Fill in the blank", answerHint: "The missing word — write the blank as _____ in the question" },
  matching: { label: "Matching", pairs: true, derivedAnswer: true },
  ordering: { label: "Ordering", options: "steps", derivedAnswer: true },
  short_answer: { label: "Short answer", keyPoints: true },
  long_answer: { label: "Long answer", keyPoints: true, longAnswer: true },
  numerical: { label: "Numerical", keyPoints: true, answerHint: "The value with its unit" },
};

export const QUESTION_TYPE_ORDER = Object.keys(QUESTION_TYPES) as QuestionType[];
