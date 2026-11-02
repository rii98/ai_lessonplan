import { describe, expect, it } from "vitest";
import type { UnitPlan } from "@/lib/api";
import { coverageGaps, tokens } from "./gaps";

describe("syllabus coverage", () => {
  it("tokenises headings like the server (numbering, stop words, markdown)", () => {
    expect([...tokens("2.1 **Ohm's law** and resistance:")]).toEqual(["ohm", "law", "resistance"]);
  });

  it("marks a topic covered once the arc mentions most of its words", () => {
    const topics = ["Chapter 4 > Electric current", "Chapter 4 > Ohm's law", "Chapter 4 > Magnetic effect of current"];
    const plan: Pick<UnitPlan, "big_idea" | "unit_outcomes" | "days"> = { big_idea: "", unit_outcomes: [], days: [{ day: 1, topic: "What is electric current?", objective_seeds: [], builds_on: "", sets_up: "" }] };
    expect([...coverageGaps(topics, plan)]).toEqual(["Chapter 4 > Ohm's law", "Chapter 4 > Magnetic effect of current"]);
    plan.days.push({ day: 2, topic: "Ohm's law in a torch", objective_seeds: ["magnetic effect"], builds_on: "", sets_up: "" });
    expect([...coverageGaps(topics, plan)]).toEqual([]);
  });
});
