import { describe, expect, it } from "vitest";
import { applyEvent, emptyTurn } from "./turn";

const run = (events: { event: string; data: unknown }[]) => events.reduce(applyEvent, emptyTurn());

describe("chat turn", () => {
  it("accumulates tokens, sources and completion", () => {
    const t = run([{ event: "token", data: "Hi " }, { event: "token", data: "there [1]" }, { event: "sources", data: [{ n: 1, source: "Book" }] }, { event: "done", data: { title: "Greeting" } }]);
    expect(t).toMatchObject({ content: "Hi there [1]", citations: [{ n: 1 }], done: true, title: "Greeting" });
  });

  it("tracks skills, status and artifacts; ignores unknown events", () => {
    const art = { id: "a1", kind: "quiz", title: "Q", question_count: 5, attempts: 0, best: null, last: null, in_progress: null };
    const t = run([{ event: "intent", data: { skill: "quiz" } }, { event: "status", data: { message: "Writing questions…" } }, { event: "mystery", data: 1 }, { event: "artifact", data: art }]);
    expect(t.skill).toBe("quiz");
    expect(t.status).toBeNull();
    expect(t.artifacts).toEqual([art]);
  });

  it("ends on an error event", () => {
    expect(run([{ event: "error", data: { message: "nope" } }])).toMatchObject({ done: true, error: "nope" });
    expect(run([{ event: "intent", data: { skill: "qa" } }]).skill).toBeNull();
  });
});
