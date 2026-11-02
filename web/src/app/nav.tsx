import type { ReactNode } from "react";
import { Icon } from "@/ui";

/**
 * Presentation for the server's `/me.nav` entries. The SERVER decides which
 * destinations a user has (it owns permissions); this only maps a path to how it
 * looks. An unknown path from a newer server still renders, with a generic icon.
 */
const PRESENTATION: Record<string, { label: string; icon: ReactNode; match: (p: string) => boolean }> = {
  "/": { label: "Plan", icon: <Icon.PenLine size={18} />, match: (p) => p === "/" || p.startsWith("/lessons") || p.startsWith("/studio") },
  "/units": { label: "Units", icon: <Icon.CalendarRange size={18} />, match: (p) => p.startsWith("/units") },
  "/library": { label: "Library", icon: <Icon.LibraryBig size={18} />, match: (p) => p.startsWith("/library") },
  "/chat": { label: "Chat", icon: <Icon.MessageSquare size={18} />, match: (p) => p.startsWith("/chat") },
  "/corpus": { label: "Knowledge", icon: <Icon.BookOpen size={18} />, match: (p) => p.startsWith("/corpus") },
  "/admin": { label: "Owner", icon: <Icon.Shield size={18} />, match: (p) => p.startsWith("/admin") },
};

export interface NavItem { path: string; label: string; icon: ReactNode; match: (pathname: string) => boolean }

export function toNavItems(nav: { path: string; label: string }[]): NavItem[] {
  return nav.map((n) => {
    const p = PRESENTATION[n.path];
    return p ? { path: n.path, ...p } : { path: n.path, label: n.label, icon: <Icon.Circle size={18} />, match: (x: string) => x.startsWith(n.path) };
  });
}
