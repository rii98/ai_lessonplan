import { describe, expect, it } from "vitest";
import { answerLabel } from "../ChatPage";
import type { LearnWidget } from "../types";

const w = { id: "c1", choices: ["warmth", "energy to make food"] } as LearnWidget;

describe("widget answer label", () => {
  it("reads like the server's transcript line", () => {
    expect(answerLabel(w, 1)).toBe("Answered: energy to make food");
    expect(answerLabel(w, ["B", "A"])).toBe("Answered: (arranged the items)");
    expect(answerLabel(w, "glucose")).toBe("Answered: glucose");
  });
});
