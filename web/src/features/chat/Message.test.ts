import { describe, expect, it } from "vitest";
import { citationLabel } from "./Message";

describe("citationLabel", () => {
  it("prefers the heading trail, hides raw ids", () => {
    expect(citationLabel({ n: 1, source: "ui:1", collection: "local_context", metadata: { heading_path: "Ch 5 > **Sound**" } })).toBe("Ch 5 › Sound");
    expect(citationLabel({ n: 1, source: "ui:1", collection: "local_context" })).toBe("local context");
    expect(citationLabel({ n: 1, source: "Science Grade 8", collection: "reference" })).toBe("Science Grade 8");
  });
});
