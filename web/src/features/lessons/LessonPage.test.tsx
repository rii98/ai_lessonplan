import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import { lessonFixture } from "@/test/fixtures";
import { renderAt } from "@/test/render";
import { API, server, teacher } from "@/test/server";
import type { LessonDesignDocument } from "@/lib/api";
import { LessonPage } from "./pages/LessonPage";

const doc = (over = {}) => ({ id: "d1", owner_id: teacher.id, owner_name: "Sita", title: "Lightning", kind: "lesson", visibility: "private",
  head_version_id: "v1", tenant_id: "sch-7", grade: 8, subject: "Science", created_at: "2026-11-01T00:00:00Z", updated_at: "2026-11-01T00:00:00Z", copied_from: null, shared_at: null, ...over });

function serve({ valid = true, owner = teacher.id } = {}) {
  const saved: LessonDesignDocument[] = [];
  server.use(
    http.get(`${API}/documents/d1`, () => HttpResponse.json({ document: doc({ owner_id: owner }), ldd: lessonFixture() })),
    http.post(`${API}/lessons/validate`, () => HttpResponse.json(valid ? { valid: true, errors: [] }
      : { valid: false, errors: [{ loc: "", msg: "Value error, objective 'O1' is not covered by any formative check" }] })),
    http.post(`${API}/documents/d1/manual`, async ({ request }) => {
      const ldd = (await request.json()) as LessonDesignDocument;
      saved.push(ldd);
      return HttpResponse.json({ document: doc({ head_version_id: "v2" }), version_id: "v2", ldd });
    }),
  );
  return saved;
}

const open = () => renderAt(<LessonPage />, { path: "/lessons/d1", pattern: "/lessons/:documentId" });

describe("lesson editor", () => {
  it("edits in place and saves the exact draft as a new version", async () => {
    const saved = serve();
    open();
    const hook = await screen.findByRole("textbox", { name: "Hook" });
    expect(screen.getByRole("button", { name: /save/i })).toBeDisabled();
    await userEvent.clear(hook);
    await userEvent.type(hook, "Count the seconds between flash and bang");
    expect(screen.getByText("Unsaved changes")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /save/i }));
    await waitFor(() => expect(saved).toHaveLength(1));
    expect(saved[0]!.engagement_hook.prompt).toBe("Count the seconds between flash and bang");
    expect(await screen.findByText("All changes saved")).toBeInTheDocument();
  });

  it("shows live guardrail problems against the section that fixes them", async () => {
    serve({ valid: false });
    open();
    expect(await screen.findByRole("button", { name: /1 to fix/ })).toBeInTheDocument();
    const checks = document.getElementById("sec-formative_checks")!;
    expect(within(checks).getByText(/not covered by any formative check/)).toBeInTheDocument();
    expect(within(checks).getAllByRole("img", { name: "Fails a guardrail" }).length).toBeGreaterThan(0);
  });

  it("renaming an objective keeps every link pointing at it", async () => {
    serve();
    open();
    const id = await screen.findByRole("textbox", { name: "Objective id" });
    await userEvent.clear(id);
    await userEvent.type(id, "O9");
    const phases = document.getElementById("sec-phases")!;
    expect(within(phases).getAllByRole("button", { name: "O9", pressed: true })).toHaveLength(2);
  });

  it("opens a colleague's shared plan read-only with a copy action", async () => {
    serve({ owner: "someone-else" });
    open();
    expect(await screen.findByText("Shared by Sita")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /copy to my work/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^save/i })).not.toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Hook" })).toHaveAttribute("readonly");
  });

  it("reviews an AI revision before applying it", async () => {
    serve();
    server.use(http.post(`${API}/lessons/refine`, () => HttpResponse.json({
      ok: true, target: "engagement_hook", instruction: "x", applied: false, notes: "",
      candidate: { ...lessonFixture(), engagement_hook: { kind: "prediction", prompt: "Predict: flash or bang first?" } },
      diff: { engagement_hook: { before: "Why does the flash come first?", after: "Predict: flash or bang first?" } },
      cascaded: [], errors: [],
    })));
    open();
    await userEvent.click(await screen.findByRole("button", { name: "Revise Hook with AI" }));
    const dialog = await screen.findByRole("dialog");
    await userEvent.type(within(dialog).getByRole("textbox"), "make it a prediction");
    await userEvent.click(within(dialog).getByRole("button", { name: /propose a change/i }));
    expect(await within(dialog).findByLabelText("After")).toHaveTextContent("Predict: flash or bang first?");
    // nothing applied yet (the page sits behind the modal, so it's aria-hidden)
    expect(screen.getByRole("textbox", { name: "Hook", hidden: true })).toHaveValue("Why does the flash come first?");
    await userEvent.click(within(dialog).getByRole("button", { name: /accept change/i }));
    expect(screen.getByRole("textbox", { name: "Hook" })).toHaveValue("Predict: flash or bang first?");
    expect(screen.getByText("Unsaved changes")).toBeInTheDocument();
  });
});
