import { describe, expect, it } from "vitest";
import { lessonFixture } from "@/test/fixtures";
import { issuesAt, toIssues } from "./issues";
import { nextId, normalizeLesson, totalMinutes } from "./ldd";

describe("toIssues", () => {
  it("pins field errors to their section and path", () => {
    const [i] = toIssues([{ loc: "phases.1.minutes", msg: "Input should be greater than 0" }]);
    expect(i).toEqual({ section: "phases", path: ["phases", 1, "minutes"], message: "Input should be greater than 0" });
  });

  it("routes whole-lesson guardrails to the section that fixes them", () => {
    const issues = toIssues([
      { loc: "", msg: "Value error, objective 'O2' is not covered by any formative check" },
      { loc: "", msg: "Value error, objective 'O3' is not addressed by any phase" },
      { loc: "curriculum_ref.grade", msg: "required" },
    ]);
    expect(issues.map((i) => i.section)).toEqual(["formative_checks", "phases", "overview"]);
    expect(issues[0]!.message).toBe("objective 'O2' is not covered by any formative check");
  });

  it("filters issues under a path", () => {
    const issues = toIssues([{ loc: "objectives.0.statement", msg: "vague" }, { loc: "objectives.1.statement", msg: "vague" }]);
    expect(issuesAt(issues, ["objectives", 1])).toHaveLength(1);
  });
});

describe("lesson model", () => {
  it("normalizes optional lists without changing meaning", () => {
    const { misconceptions: _m, homework: _h, ...bare } = lessonFixture();
    const l = normalizeLesson(bare);
    expect(l.misconceptions).toEqual([]);
    expect(l.homework).toBeNull();
    expect(l.formative_checks[0]!.key_points).toEqual([]);
  });

  it("finds the next free id and totals minutes", () => {
    expect(nextId("O", ["O1", "O3"])).toBe("O2");
    expect(totalMinutes(lessonFixture().phases)).toBe(45);
  });
});
