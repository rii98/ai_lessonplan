import { describe, expect, it } from "vitest";
import { renderRich } from "./render";

describe("renderRich", () => {
  it("links single and grouped citations, never inside code", () => {
    const box = document.createElement("div");
    box.innerHTML = renderRich("Light [1] and sound [2, 3]. `arr[4]`", { citations: true });
    expect([...box.querySelectorAll(".cite")].map((b) => b.textContent)).toEqual(["1", "2", "3"]);
  });

  it("sanitizes model HTML and renders math", () => {
    const html = renderRich('<img src=x onerror="alert(1)"> $E=mc^2$');
    expect(html).not.toContain("onerror");
    expect(html).toContain("katex");
  });
});
