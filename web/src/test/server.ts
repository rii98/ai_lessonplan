import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import type { Me } from "@/features/session";

export const API = "http://localhost/api/v1";

export const teacher: Me = {
  id: "p-teacher", subject: "teacher:7", issuer: "edboox", name: "Sita Sharma", role: "teacher", roles: ["teacher"],
  permissions: ["author", "chat"], tenant_id: "sch-7", context: null, scope_mode: "prefill", knowledge_spaces: ["global"],
  nav: [{ path: "/", label: "Lesson editor" }, { path: "/units", label: "Units" }, { path: "/library", label: "Library" }, { path: "/chat", label: "Chat" }],
  logout_url: "/auth/logout",
};
export const student: Me = { ...teacher, id: "p-student", name: "Aarav", role: "student", roles: ["student"], permissions: ["chat"], nav: [{ path: "/chat", label: "Chat" }] };

/** Defaults every screen needs; tests add their own with `server.use(...)`. */
export const server = setupServer(
  http.get(`${API}/me`, () => HttpResponse.json(teacher)),
  http.get(`${API}/profile`, () => HttpResponse.json({ owner_id: "p-teacher", teacher_name: "Sita Sharma", default_grade: 8, default_subject: "Science", local_anchors: [] })),
);
