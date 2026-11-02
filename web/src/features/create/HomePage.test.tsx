import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import { lessonFixture } from "@/test/fixtures";
import { renderAt } from "@/test/render";
import { API, server } from "@/test/server";
import { HomePage } from "./HomePage";

describe("home composer", () => {
  it("writes a lesson, saves it, and opens it in the editor", async () => {
    let body: Record<string, unknown> | null = null;
    server.use(
      http.get(`${API}/documents`, () => HttpResponse.json({ items: [] })),
      http.post(`${API}/documents/lessons`, async ({ request }) => {
        body = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({ document: { id: "new-1", owner_id: "p-teacher", title: "T", kind: "lesson" }, version_id: "v1", ldd: lessonFixture() });
      }),
    );
    const { router } = renderAt(<HomePage />, { extra: [{ path: "/lessons/:id", element: <div>editor</div> }] });
    expect(await screen.findByRole("heading", { name: /, Sita\.$/ })).toBeInTheDocument();
    await userEvent.type(screen.getByRole("textbox", { name: "What are you teaching?" }), "Lightning and thunder");
    await userEvent.click(screen.getByRole("button", { name: "Write the lesson" }));
    await waitFor(() => expect(router.state.location.pathname).toBe("/lessons/new-1"));
    expect(body).toMatchObject({ topic: "Lightning and thunder", grade: null, existing_plan: null });
  });

  it("hands a unit brief to the planner with profile defaults", async () => {
    server.use(http.get(`${API}/documents`, () => HttpResponse.json({ items: [] })));
    const { router } = renderAt(<HomePage />, { extra: [{ path: "/units/new", element: <div>planner</div> }] });
    await userEvent.click(await screen.findByRole("radio", { name: "Unit" }));
    await userEvent.type(screen.getByRole("textbox", { name: "What are you teaching?" }), "Electricity");
    await userEvent.click(screen.getByRole("button", { name: "Plan the unit" }));
    await waitFor(() => expect(router.state.location.pathname).toBe("/units/new"));
    expect(router.state.location.state).toMatchObject({ autoplan: true, brief: { topic: "Electricity", grade: 8, subject: "Science", num_days: 5 } });
  });

  it("asks for a topic instead of sending an empty brief", async () => {
    server.use(http.get(`${API}/documents`, () => HttpResponse.json({ items: [] })));
    renderAt(<HomePage />);
    await userEvent.click(await screen.findByRole("button", { name: "Write the lesson" }));
    expect(screen.getByText("Tell us what you're teaching first.")).toBeInTheDocument();
  });
});
