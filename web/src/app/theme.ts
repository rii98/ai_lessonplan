import { safeStorage } from "@/lib/hooks";

export type Theme = "light" | "dark" | "system";
const KEY = "lf.theme";

export function setTheme(t: Theme) {
  if (t === "system") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.setAttribute("data-theme", t);
  safeStorage.set(KEY, t);
}

export function applyStoredTheme() {
  const t = safeStorage.get<Theme>(KEY);
  if (t && t !== "system") document.documentElement.setAttribute("data-theme", t);
}
