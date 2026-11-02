import { act, renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { useDraft } from "./useDraft";

describe("useDraft", () => {
  it("tracks dirty state, undo and redo", () => {
    const { result } = renderHook(() => useDraft({ a: 1, list: ["x"] }));
    expect(result.current.dirty).toBe(false);
    act(() => result.current.update((d) => { d.a = 2; }));
    act(() => result.current.update((d) => { d.list.push("y"); }));
    expect(result.current.value).toEqual({ a: 2, list: ["x", "y"] });
    expect(result.current.dirty).toBe(true);
    act(() => result.current.undo());
    expect(result.current.value.list).toEqual(["x"]);
    act(() => result.current.redo());
    expect(result.current.value.list).toEqual(["x", "y"]);
  });

  it("folds rapid edits to one field into a single undo step", () => {
    const { result } = renderHook(() => useDraft({ t: "" }));
    for (const v of ["h", "he", "hel"]) act(() => result.current.update((d) => { d.t = v; }, "t"));
    act(() => result.current.undo());
    expect(result.current.value.t).toBe("");
  });

  it("accepts a whole replacement value as one undoable step (an AI proposal)", () => {
    const { result } = renderHook(() => useDraft({ t: "mine" }));
    act(() => result.current.update(() => ({ t: "ai" })));
    expect(result.current.value.t).toBe("ai");
    act(() => result.current.undo());
    expect(result.current.value.t).toBe("mine");
  });

  it("stays dirty for edits made after the saved revision", () => {
    const { result } = renderHook(() => useDraft({ n: 0 }));
    act(() => result.current.update((d) => { d.n = 1; }));
    const rev = result.current.revision;
    act(() => result.current.update((d) => { d.n = 2; }));   // typed while saving
    act(() => result.current.markSaved(rev));
    expect(result.current.dirty).toBe(true);
    act(() => result.current.markSaved(result.current.revision));
    expect(result.current.dirty).toBe(false);
  });
});
