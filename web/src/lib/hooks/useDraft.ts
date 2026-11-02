import { applyPatches, enablePatches, produceWithPatches, type Draft, type Patch } from "immer";
import { useCallback, useMemo, useReducer } from "react";

enablePatches();

interface Entry {
  redo: Patch[];
  undo: Patch[];
  /** Edits sharing a key within COALESCE_MS fold into this one undo step. */
  key?: string;
  at: number;
}

interface State<T> {
  value: T;
  /** Bumped on every change — cheap "changed since X" checks. */
  revision: number;
  /** Revision of the last committed (loaded / saved) value. */
  savedRevision: number;
  past: Entry[];
  future: Entry[];
}

/** A recipe mutates the draft, or returns a whole new value (e.g. an accepted AI proposal). */
export type Recipe<T> = (draft: Draft<T>) => void | T;

type Action<T> =
  | { type: "update"; recipe: Recipe<T>; key?: string; now: number }
  | { type: "reset"; value: T }
  | { type: "markSaved"; revision: number }
  | { type: "undo" }
  | { type: "redo" };

const HISTORY_LIMIT = 200;
const COALESCE_MS = 1200;

function init<T>(value: T): State<T> {
  return { value, revision: 0, savedRevision: 0, past: [], future: [] };
}

export function draftReducer<T>(s: State<T>, a: Action<T>): State<T> {
  switch (a.type) {
    case "update": {
      const [value, redo, undo] = produceWithPatches(s.value, a.recipe as (d: Draft<T>) => void);
      if (!redo.length) return s;
      const last = s.past[s.past.length - 1];
      const merge = a.key !== undefined && last?.key === a.key && a.now - last.at < COALESCE_MS;
      const past = merge
        ? [...s.past.slice(0, -1), { key: a.key, at: a.now, redo: [...last.redo, ...redo], undo: [...undo, ...last.undo] }]
        : [...s.past, { key: a.key, at: a.now, redo, undo }].slice(-HISTORY_LIMIT);
      return { ...s, value: value as T, revision: s.revision + 1, past, future: [] };
    }
    case "reset": {
      const revision = s.revision + 1;
      return { value: a.value, revision, savedRevision: revision, past: [], future: [] };
    }
    case "markSaved":
      return { ...s, savedRevision: a.revision };
    case "undo": {
      const entry = s.past[s.past.length - 1];
      if (!entry) return s;
      return { ...s, value: applyPatches(s.value as object, entry.undo) as T, revision: s.revision + 1,
        past: s.past.slice(0, -1), future: [{ ...entry, key: undefined }, ...s.future] };
    }
    case "redo": {
      const [entry, ...rest] = s.future;
      if (!entry) return s;
      return { ...s, value: applyPatches(s.value as object, entry.redo) as T, revision: s.revision + 1,
        past: [...s.past, entry], future: rest };
    }
  }
}

/**
 * A locally edited document with undo/redo and dirty tracking.
 *
 * Edits are immer recipes, so components "mutate" a draft while React still gets
 * a new immutable value. Undo is patch-based — cheap for large documents — and
 * consecutive edits sharing a `key` (typing in one field) fold into one step.
 */
export function useDraft<T>(initial: T) {
  const [state, dispatch] = useReducer(draftReducer<T>, initial, init<T>);

  const update = useCallback((recipe: Recipe<T>, key?: string) =>
    dispatch({ type: "update", recipe, key, now: Date.now() }), []);
  /** Replace the value and treat it as the committed baseline (load / reload). */
  const reset = useCallback((value: T) => dispatch({ type: "reset", value }), []);
  /** The value at `revision` has been persisted. Later edits stay dirty. */
  const markSaved = useCallback((revision: number) => dispatch({ type: "markSaved", revision }), []);
  const undo = useCallback(() => dispatch({ type: "undo" }), []);
  const redo = useCallback(() => dispatch({ type: "redo" }), []);

  return useMemo(() => ({
    value: state.value,
    revision: state.revision,
    dirty: state.revision !== state.savedRevision,
    canUndo: state.past.length > 0,
    canRedo: state.future.length > 0,
    update, reset, markSaved, undo, redo,
  }), [state, update, reset, markSaved, undo, redo]);
}

export type DraftApi<T> = ReturnType<typeof useDraft<T>>;
