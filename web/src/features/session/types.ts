/** GET /api/v1/me — contract gap: the route returns an untyped dict. */
export interface Me {
  id: string;
  subject: string;
  issuer: string;
  name: string | null;
  role: string;
  roles: string[];
  permissions: string[];
  tenant_id: string | null;
  context: Record<string, unknown> | null;
  scope_mode: string;
  knowledge_spaces: "*" | string[];
  nav: { path: string; label: string }[];
  logout_url: string;
}

/** Server permission names (auth/principal.py `Perm`). */
export const Perm = {
  chat: "chat",
  author: "author",
  corpus: "corpus.manage",
  ops: "ops",
  reports: "reports.read",
  admin: "admin",
} as const;
export type PermName = (typeof Perm)[keyof typeof Perm];
