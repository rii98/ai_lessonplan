import type { ReactNode } from "react";
import { Navigate } from "react-router-dom";
import { canIn, useMe, type PermName } from "@/features/session";
import { EmptyState, Icon } from "@/ui";

/**
 * Render `children` only for users holding `perm`; otherwise send them to the
 * first destination the server says they have (a student opening the lesson
 * editor lands in Chat). The server enforces the same rule on every API call —
 * this is UX, not security.
 */
export function RequirePerm({ perm, children }: { perm: PermName; children: ReactNode }) {
  const { data: me } = useMe();
  if (!me) return null;
  if (canIn(me, perm)) return <>{children}</>;
  const home = me.nav[0]?.path;
  if (home && home !== window.location.pathname) return <Navigate to={home} replace />;
  return <EmptyState icon={<Icon.Lock size={36} />} title="You don't have access to this">Ask your school's LessonForge owner if you think you should.</EmptyState>;
}
