import type { ComponentType } from "react";
import { createBrowserRouter, type RouteObject } from "react-router-dom";
import { Perm, type PermName } from "@/features/session";
import { AppShell } from "./AppShell";
import { RequirePerm } from "./guards";
import { NotFound, RouteError } from "./RouteError";

/**
 * The route table. Each entry names the permission it needs and loads its feature
 * on demand (one chunk per feature — a student never downloads the editor).
 * Adding a screen = one line here.
 */
interface RouteDef {
  path: string;
  perm: PermName;
  load: () => Promise<ComponentType>;
}

const ROUTES: RouteDef[] = [
  { path: "/", perm: Perm.author, load: () => import("@/features/create").then((m) => m.HomePage) },
  { path: "/lessons/:documentId", perm: Perm.author, load: () => import("@/features/lessons").then((m) => m.LessonPage) },
  { path: "/units", perm: Perm.author, load: () => import("@/features/units").then((m) => m.UnitsIndexPage) },
  { path: "/units/new", perm: Perm.author, load: () => import("@/features/units").then((m) => m.UnitPlannerPage) },
  { path: "/units/:unitId", perm: Perm.author, load: () => import("@/features/units").then((m) => m.UnitPage) },
  { path: "/units/:unitId/days/:day", perm: Perm.author, load: () => import("@/features/units").then((m) => m.UnitDayPage) },
  { path: "/studio/:kind", perm: Perm.author, load: () => import("@/features/studio").then((m) => m.StudioPage) },
  { path: "/library", perm: Perm.author, load: () => import("@/features/library").then((m) => m.LibraryPage) },
  { path: "/settings", perm: Perm.author, load: () => import("@/features/profile").then((m) => m.ProfilePage) },
  { path: "/chat/:conversationId?", perm: Perm.chat, load: () => import("@/features/chat").then((m) => m.ChatPage) },
  { path: "/corpus", perm: Perm.corpus, load: () => import("@/features/knowledge").then((m) => m.KnowledgePage) },
  { path: "/admin/*", perm: Perm.admin, load: () => import("@/features/admin").then((m) => m.AdminPage) },
];

function toRoute({ path, perm, load }: RouteDef): RouteObject {
  return {
    path,
    errorElement: <RouteError />,
    lazy: async () => {
      const Screen = await load();
      return { Component: () => <RequirePerm perm={perm}><Screen /></RequirePerm> };
    },
  };
}

export function createRouter(basename?: string) {
  return createBrowserRouter([
    {
      element: <AppShell />,
      errorElement: <RouteError />,
      children: [...ROUTES.map(toRoute), { path: "*", element: <NotFound /> }],
    },
  ], { basename });
}
