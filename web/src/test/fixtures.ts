import type { LessonDesignDocument } from "@/lib/api";

export const lessonFixture = (): LessonDesignDocument => ({
  topic: "Why we see lightning before thunder",
  curriculum_ref: { board: "CDC", grade: 8, subject: "Science" },
  duration_min: 45, language: "en-ne", framework: "5E",
  objectives: [{ id: "O1", statement: "Explain that light travels faster than sound", bloom: "understand" }],
  prior_knowledge: ["sound needs a medium"], misconceptions: [], local_context: ["monsoon storms"], materials: [],
  engagement_hook: { kind: "question", prompt: "Why does the flash come first?" },
  phases: [
    { name_en: "Engage", name_ne: "", teacher_activities: ["Ask the hook"], student_activities: ["Predict"], minutes: 20, objective_ids: ["O1"] },
    { name_en: "Explain", name_ne: "", teacher_activities: ["Compare speeds"], student_activities: ["Calculate"], minutes: 25, objective_ids: ["O1"] },
  ],
  differentiation: { struggling: [], on_level: [], advanced: [] },
  formative_checks: [{ id: "Q1", type: "short_answer", prompt: "Which is faster?", answer: "Light", explanation: "", objective_ids: ["O1"], key_points: [] }],
  homework: null,
  quality: { engagement: 0.8, alignment: 0.9, misconception_coverage: 0.4, specificity: 0.7, local_relevance: 0.6, notes: "", grounding_sources: [], adjustments: [] },
});
