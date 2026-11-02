import type { ComponentType } from "react";
import { plural, truncate } from "@/lib/format";
import type { Status } from "@/ui";
import type { Issue } from "../../model/issues";
import { totalMinutes, type Lesson } from "../../model/ldd";
import type { SectionProps } from "../parts";
import { ChecksSection, DifferentiationSection, HomeworkSection } from "./Assessment";
import { HookSection } from "./Hook";
import { chipSection, MisconceptionsSection } from "./Lists";
import { ObjectivesSection } from "./Objectives";
import { OverviewSection } from "./Overview";
import { PhasesSection } from "./Phases";

/**
 * Every part of the lesson, in document order. The outline, the status dots,
 * issue routing and the editor body all read this list — adding a section to the
 * LDD means adding one entry here (and its component).
 */
export interface SectionDef {
  key: string;
  title: string;
  hint?: string;
  /** Reprompt target for "Revise with AI" (server: /lessons/refine/targets). */
  refine?: string;
  /** Collapsed by default — supporting detail, not the spine of the lesson. */
  secondary?: boolean;
  summary: (l: Lesson) => string;
  /** Status when the server reports no guardrail issue for it. */
  status: (l: Lesson) => Status;
  Component: ComponentType<SectionProps>;
}

const listStatus = (n: number): Status => (n ? "ok" : "empty");

export const SECTIONS: SectionDef[] = [
  {
    key: "overview", title: "Class & period", Component: OverviewSection,
    summary: (l) => `Grade ${l.curriculum_ref.grade} · ${l.curriculum_ref.subject} · ${l.duration_min} min`,
    status: (l) => (l.curriculum_ref.subject.trim() && l.curriculum_ref.grade ? "ok" : "attention"),
  },
  {
    key: "engagement_hook", title: "Hook", refine: "engagement_hook", Component: HookSection,
    hint: "Open with curiosity — a scene, question, prediction or demonstration. Never a definition.",
    summary: (l) => truncate(l.engagement_hook.prompt, 60) || "Empty",
    status: (l) => (l.engagement_hook.prompt.trim() ? "ok" : "attention"),
  },
  {
    key: "objectives", title: "Objectives", refine: "objectives", Component: ObjectivesSection,
    hint: "Each one must be taught in a phase and checked by a question.",
    summary: (l) => plural(l.objectives.length, "objective"),
    status: (l) => (l.objectives.length && l.objectives.every((o) => o.statement.trim().split(/\s+/).length >= 3) ? "ok" : "attention"),
  },
  {
    key: "phases", title: "Teaching sequence", refine: "phases", Component: PhasesSection,
    summary: (l) => `${plural(l.phases.length, "phase")} · ${totalMinutes(l.phases)}/${l.duration_min} min`,
    status: (l) => (l.phases.length && totalMinutes(l.phases) === l.duration_min ? "ok" : "attention"),
  },
  {
    key: "formative_checks", title: "Checks for understanding", refine: "formative_checks", Component: ChecksSection,
    summary: (l) => plural(l.formative_checks.length, "question"),
    status: (l) => {
      const assessed = new Set(l.formative_checks.flatMap((q) => q.objective_ids));
      return l.formative_checks.length && l.objectives.every((o) => assessed.has(o.id)) ? "ok" : "attention";
    },
  },
  {
    key: "misconceptions", title: "Misconceptions", refine: "misconceptions", Component: MisconceptionsSection,
    hint: "The wrong ideas students at this grade bring — and how you'll turn them around.",
    summary: (l) => plural(l.misconceptions.length, "misconception"), status: (l) => listStatus(l.misconceptions.length),
  },
  {
    key: "prior_knowledge", title: "Prior knowledge", refine: "prior_knowledge", secondary: true,
    Component: chipSection("prior_knowledge", "What they already know…"),
    summary: (l) => plural(l.prior_knowledge.length, "item"), status: (l) => listStatus(l.prior_knowledge.length),
  },
  {
    key: "local_context", title: "Local context", refine: "local_context", secondary: true,
    hint: "Places, crops, festivals, daily life your students recognise — what makes the lesson theirs.",
    Component: chipSection("local_context", "e.g. the village tap, Tihar…"),
    summary: (l) => plural(l.local_context.length, "anchor"), status: (l) => listStatus(l.local_context.length),
  },
  {
    key: "materials", title: "Materials", refine: "materials", secondary: true,
    Component: chipSection("materials", "e.g. a glass of water…"),
    summary: (l) => plural(l.materials.length, "item"), status: (l) => listStatus(l.materials.length),
  },
  {
    key: "differentiation", title: "Differentiation", refine: "differentiation", secondary: true, Component: DifferentiationSection,
    summary: (l) => plural(l.differentiation.struggling.length + l.differentiation.on_level.length + l.differentiation.advanced.length, "strategy", "strategies"),
    status: (l) => listStatus(l.differentiation.struggling.length + l.differentiation.on_level.length + l.differentiation.advanced.length),
  },
  {
    key: "homework", title: "Homework", refine: "homework", secondary: true, Component: HomeworkSection,
    summary: (l) => (l.homework ? plural(l.homework.instructions.length, "task") : "None"),
    status: (l) => (l.homework ? "ok" : "empty"),
  },
];

export const SECTION_TITLE: Record<string, string> = Object.fromEntries([...SECTIONS.map((s) => [s.key, s.title]), ["*", "the whole lesson"]]);

export function sectionStatus(def: SectionDef, lesson: Lesson, issues: Issue[]): Status {
  return issues.some((i) => i.section === def.key) ? "error" : def.status(lesson);
}
