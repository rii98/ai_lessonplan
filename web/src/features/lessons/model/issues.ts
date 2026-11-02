import type { FieldIssue } from "@/lib/api";

/** A guardrail problem pinned to a section (and, when known, a path inside it). */
export interface Issue {
  section: string;
  /** Path inside the lesson, e.g. ["phases", 2, "minutes"]; [] for section-wide. */
  path: (string | number)[];
  message: string;
}

const MODEL_RULES: [RegExp, string][] = [
  [/not addressed by any phase|^phase /i, "phases"],
  [/not covered by any formative check|^question /i, "formative_checks"],
  [/objective ids must be unique/i, "objectives"],
];

const clean = (msg: string) => msg.replace(/^Value error,\s*/i, "").replace(/^Assertion failed,\s*/i, "");

/**
 * Turn server validation errors (`{loc: "phases.0.minutes", msg}`) into issues the
 * editor can pin to a section. Whole-lesson guardrails (loc "") are routed by
 * their message — "objective O2 is not covered by any formative check" belongs
 * to the checks section, where it can be fixed.
 */
export function toIssues(errors: FieldIssue[]): Issue[] {
  return errors.map(({ loc, msg }) => {
    const path = loc ? loc.split(".").map((p) => (/^\d+$/.test(p) ? Number(p) : p)) : [];
    const message = clean(msg);
    let section = typeof path[0] === "string" ? path[0] : "";
    if (section === "curriculum_ref" || section === "topic" || section === "duration_min" || section === "language" || section === "framework") section = "overview";
    if (!section) section = MODEL_RULES.find(([re]) => re.test(message))?.[1] ?? "overview";
    return { section, path, message };
  });
}

/** Issues at or under `prefix` (e.g. ["phases", 2]). */
export const issuesAt = (issues: Issue[], prefix: (string | number)[]) =>
  issues.filter((i) => prefix.every((p, k) => i.path[k] === p));
