import type { UnitPlan } from "@/lib/api";

/**
 * Which chapter topics the (edited) arc covers — recomputed on every keystroke
 * so covering a gap turns it green immediately. Mirrors the server's
 * `coverage_gaps()` token matcher, so the strip and the API agree.
 */
const STOP = new Set("the a an of to in on and or for with without into from by as is are be its this that these those study regarding".split(" "));

export function tokens(s: string | null | undefined): Set<string> {
  let t = String(s ?? "").replace(/[*`_#]+/g, "");
  t = t.replace(/^\s*(?:\d+(?:\.\d+)*\.?|[a-zA-Z]|[ivxIVX]+)[.)]\s+/, "");
  t = t.replace(/[:.]+$/, "").trim().toLowerCase();
  return new Set(t.split(/[^a-z0-9]+/).filter((w) => w.length > 2 && !STOP.has(w)));
}

export function coverageGaps(topics: string[], plan: Pick<UnitPlan, "big_idea" | "unit_outcomes" | "days">): Set<string> {
  const hay = new Set<string>();
  const add = (x: string | null | undefined) => tokens(x).forEach((w) => hay.add(w));
  add(plan.big_idea);
  (plan.unit_outcomes ?? []).forEach(add);
  for (const d of plan.days) { add(d.topic); (d.objective_seeds ?? []).forEach(add); }
  return new Set(topics.filter((sec) => {
    const want = tokens(sec.split(" > ").pop());
    if (!want.size) return false;
    let hit = 0;
    want.forEach((w) => { if (hay.has(w)) hit++; });
    return hit / want.size < 0.5;
  }));
}

export const topicLabel = (t: string) => t.split(" > ").pop() ?? t;
