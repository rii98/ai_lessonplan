import { useCallback, useState } from "react";

/** Storage access that never throws (private mode, blocked site data, quotas). */
export const safeStorage = {
  get<T>(key: string, store: Storage | undefined = globalThis.localStorage): T | null {
    try {
      const raw = store?.getItem(key);
      return raw ? (JSON.parse(raw) as T) : null;
    } catch {
      return null;
    }
  },
  set(key: string, value: unknown, store: Storage | undefined = globalThis.localStorage): void {
    try {
      store?.setItem(key, JSON.stringify(value));
    } catch {
      /* best effort */
    }
  },
  remove(key: string, store: Storage | undefined = globalThis.localStorage): void {
    try {
      store?.removeItem(key);
    } catch {
      /* best effort */
    }
  },
};

/** `useState` remembered per browser — for conveniences only (a tab, a filter). */
export function useStoredState<T>(key: string, initial: T): [T, (v: T) => void] {
  const [v, setV] = useState<T>(() => safeStorage.get<T>(key) ?? initial);
  const set = useCallback((next: T) => { setV(next); safeStorage.set(key, next); }, [key]);
  return [v, set];
}
