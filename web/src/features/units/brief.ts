import type { UnitRequest } from "@/lib/api";

/** What the planner starts from — what Home hands over in router state. */
export interface UnitBrief {
  topic: string;
  grade: number | null;
  subject: string;
  num_days: number;
  duration_min: number;
  language: UnitRequest["language"];
  framework: UnitRequest["framework"];
  title?: string;
}

export interface UnitPlannerState { brief?: Partial<UnitBrief>; autoplan?: boolean }

export const missingFields = (b: UnitBrief) =>
  [!b.topic.trim() && "topic", b.grade == null && "grade", !b.subject.trim() && "subject"].filter(Boolean) as string[];

export const toRequest = (b: UnitBrief): UnitRequest => ({
  topic: b.topic.trim(), grade: b.grade!, subject: b.subject.trim(), num_days: b.num_days,
  duration_min: b.duration_min, language: b.language, framework: b.framework,
});
