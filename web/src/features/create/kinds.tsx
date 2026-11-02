import type { ReactNode } from "react";
import { Icon } from "@/ui";

export type CreateKind = "lesson" | "unit" | "quiz" | "worksheet" | "slides";

/** What Home can make. Each kind says what it is and how the brief continues. */
export const KINDS: Record<CreateKind, { label: string; icon: ReactNode; placeholder: string; hint: string; cta: string }> = {
  lesson: {
    label: "Lesson", icon: <Icon.FileText size={16} />, cta: "Write the lesson",
    placeholder: "What are you teaching? e.g. Why do we see lightning before we hear thunder?",
    hint: "A full plan — hook, objectives, phases, checks — grounded in your curriculum. Saved to your library.",
  },
  unit: {
    label: "Unit", icon: <Icon.CalendarRange size={16} />, cta: "Plan the unit",
    placeholder: "Which chapter? e.g. Electricity and magnetism over a week",
    hint: "Several days as one arc. You shape the arc before every day is written.",
  },
  quiz: {
    label: "Quiz", icon: <Icon.ListChecks size={16} />, cta: "Choose the questions",
    placeholder: "A quiz on… e.g. the water cycle",
    hint: "Pick question types, how many and how hard — then edit and print.",
  },
  worksheet: {
    label: "Worksheet", icon: <Icon.ClipboardList size={16} />, cta: "Choose the questions",
    placeholder: "A worksheet on… e.g. fractions with local market prices",
    hint: "Tasks and questions with an answer key.",
  },
  slides: {
    label: "Slides", icon: <Icon.Presentation size={16} />, cta: "Make the slides",
    placeholder: "Slides for… e.g. the parts of a flower",
    hint: "A hook-first deck you can edit before downloading.",
  },
};

export const KIND_ORDER: CreateKind[] = ["lesson", "unit", "quiz", "worksheet", "slides"];
