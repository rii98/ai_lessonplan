import { useEffect, useRef } from "react";

const isMac = typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform);

/** True when focus is in something that handles its own keys (text fields). */
export function isEditableTarget(t: EventTarget | null): boolean {
  const el = t as HTMLElement | null;
  return !!el && (el.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(el.tagName));
}

/**
 * Bind a shortcut like "mod+s" or "mod+shift+z" (mod = ⌘ on Apple, Ctrl elsewhere).
 * `inFields` lets it fire while typing (Save should; Undo shouldn't — the field
 * has its own native undo).
 */
export function useHotkey(combo: string, handler: (e: KeyboardEvent) => void,
                          { enabled = true, inFields = false } = {}) {
  const ref = useRef(handler);
  ref.current = handler;
  useEffect(() => {
    if (!enabled) return;
    const parts = combo.toLowerCase().split("+");
    const key = parts[parts.length - 1];
    const wantMod = parts.includes("mod"), wantShift = parts.includes("shift");
    const onKey = (e: KeyboardEvent) => {
      const mod = isMac ? e.metaKey : e.ctrlKey;
      if (mod !== wantMod || e.shiftKey !== wantShift || e.key.toLowerCase() !== key) return;
      if (!inFields && isEditableTarget(e.target)) return;
      e.preventDefault();
      ref.current(e);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [combo, enabled, inFields]);
}

export const modKeyLabel = isMac ? "⌘" : "Ctrl";
