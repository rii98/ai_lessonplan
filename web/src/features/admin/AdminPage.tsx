import type { ReactNode } from "react";
import { NavLink, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { Icon, Page, PageHeader } from "@/ui";
import { Audit, Maintenance, Overview, Owners, People, Person, Sources, Usage } from "./views";
import s from "./admin.module.css";

/** The console's sections — adding one is one entry (open/closed). */
const SECTIONS: { path: string; label: string; icon: ReactNode; element: ReactNode }[] = [
  { path: "overview", label: "Overview", icon: <Icon.Gauge size={16} />, element: <Overview /> },
  { path: "people", label: "People", icon: <Icon.Users size={16} />, element: <People /> },
  { path: "owners", label: "Owners", icon: <Icon.KeyRound size={16} />, element: <Owners /> },
  { path: "knowledge", label: "Knowledge", icon: <Icon.Database size={16} />, element: <Sources /> },
  { path: "usage", label: "Usage", icon: <Icon.BarChart3 size={16} />, element: <Usage /> },
  { path: "audit", label: "Audit log", icon: <Icon.ScrollText size={16} />, element: <Audit /> },
  { path: "maintenance", label: "Maintenance", icon: <Icon.Wrench size={16} />, element: <Maintenance /> },
];

/** `/admin/*` — run LessonForge without a terminal. */
export function AdminPage() {
  const { pathname } = useLocation();
  const current = SECTIONS.find((x) => pathname.startsWith(`/admin/${x.path}`));
  return (
    <Page width="wide">
      <PageHeader eyebrow="Owner console" title={current?.label ?? "Overview"} />
      <div className={s.console}>
        <nav className={s.sub} aria-label="Console sections">
          {SECTIONS.map((x) => (
            <NavLink key={x.path} to={`/admin/${x.path}`} aria-current={current === x ? "page" : undefined}>{x.icon}{x.label}</NavLink>
          ))}
        </nav>
        <div style={{ minWidth: 0 }}>
          <Routes>
            <Route index element={<Navigate to="overview" replace />} />
            {SECTIONS.map((x) => <Route key={x.path} path={x.path} element={x.element} />)}
            <Route path="people/:id" element={<Person />} />
            <Route path="*" element={<Navigate to="overview" replace />} />
          </Routes>
        </div>
      </div>
    </Page>
  );
}
