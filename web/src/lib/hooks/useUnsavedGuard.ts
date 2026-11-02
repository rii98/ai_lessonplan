import { useEffect } from "react";
import { useBlocker } from "react-router-dom";

/**
 * Ask before leaving with unsaved work — both in-app navigation (router blocker)
 * and closing/reloading the tab (beforeunload). Returns the blocker so the page
 * can render its own confirm dialog.
 */
export function useUnsavedGuard(dirty: boolean) {
  useEffect(() => {
    if (!dirty) return;
    const onBeforeUnload = (e: BeforeUnloadEvent) => { e.preventDefault(); e.returnValue = ""; };
    window.addEventListener("beforeunload", onBeforeUnload);
    return () => window.removeEventListener("beforeunload", onBeforeUnload);
  }, [dirty]);
  return useBlocker(({ currentLocation, nextLocation }) =>
    dirty && currentLocation.pathname !== nextLocation.pathname);
}
