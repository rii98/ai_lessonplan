import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import { renderAt } from "@/test/render";
import { API, server } from "@/test/server";
import { ChatPage } from "./ChatPage";

const sse = (frames: [string, unknown][]) => {
  const enc = new TextEncoder();
  return new HttpResponse(new ReadableStream({
    start(c) { for (const [e, d] of frames) c.enqueue(enc.encode(`event: ${e}\ndata: ${JSON.stringify(d)}\n\n`)); c.close(); },
  }), { headers: { "content-type": "text/event-stream" } });
};

describe("chat", () => {
  it("creates a conversation, streams a cited answer and opens the source", async () => {
    const conv = { id: "c1", owner_id: "p-teacher", title: "New chat", defaults: { mode: "scoped" }, created_at: "2026-11-02T00:00:00Z", updated_at: "2026-11-02T00:00:00Z", message_count: 0, summary: "" };
    let sent: Record<string, unknown> | null = null;
    server.use(
      http.get(`${API}/chat/config`, () => HttpResponse.json({ collections: ["reference", "pedagogical"], filter_fields: ["grade"], default_mode: "scoped", scope_mode: "prefill", scope_defaults: { grade: 8 } })),
      http.get(`${API}/chat/conversations`, () => HttpResponse.json({ items: [] })),
      http.post(`${API}/chat/conversations`, () => HttpResponse.json(conv)),
      http.get(`${API}/chat/conversations/c1`, () => HttpResponse.json({ conversation: conv, messages: [], artifacts: [] })),
      http.get(`${API}/chat/conversations/c1/learning-session`, () => HttpResponse.json({ session: null })),
      http.post(`${API}/chat/conversations/c1/message`, async ({ request }) => {
        sent = (await request.json()) as Record<string, unknown>;
        await new Promise((r) => setTimeout(r, 150));   // still streaming after the route change
        return sse([["token", "Light is faster "], ["token", "than sound [1]."], ["sources", [{ n: 1, source: "Science 8, ch. 5", collection: "reference", text: "Light travels at 300,000 km/s." }]], ["done", { title: "Light vs sound" }]]);
      }),
    );
    const { router } = renderAt(<ChatPage />, { path: "/chat", pattern: "/chat/:conversationId?" });
    const box = await screen.findByRole("textbox", { name: "Message" });
    await userEvent.type(box, "Why do we see lightning first?{Enter}");
    await waitFor(() => expect(router.state.location.pathname).toBe("/chat/c1"));
    // the live turn survives the navigation to the new conversation
    expect(screen.getByText("Why do we see lightning first?")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Stop" })).toBeInTheDocument();
    // once done, the persisted thread replaces the live turn — and the chat has its server-given title
    await waitFor(() => expect(screen.getByText("Light vs sound")).toBeInTheDocument());
    expect(screen.getByText(/Light is faster than sound/)).toBeInTheDocument();
    expect(sent).toMatchObject({ message: "Why do we see lightning first?", mode: "scoped", filters: { grade: 8 } });
    await userEvent.click(screen.getByRole("button", { name: /Science 8, ch. 5/ }));
    const sheet = await screen.findByRole("dialog");
    expect(within(sheet).getByText("Light travels at 300,000 km/s.")).toBeInTheDocument();
  });

  it("learn mode: starts a lesson, shows progress + an inline check, grades the answer", async () => {
    const conv = { id: "c2", owner_id: "p-student", title: "New chat", defaults: { mode: "scoped" }, created_at: "2026-11-02T00:00:00Z", updated_at: "2026-11-02T00:00:00Z", message_count: 0, summary: "" };
    const state = (status: string, done: boolean) => ({ id: "s1", conversation_id: "c2", status: "active", goal: "Explain how plants make food", topic: "photosynthesis",
      ungrounded: false, pace: "normal", cursor: done ? "O2" : "O1", mastered: done ? 1 : 0, total: 2, parking_lot: [], choices: [],
      objectives: [{ id: "O1", title: "Plants need sunlight", status, early: false, mastery: 0.2 }, { id: "O2", title: "Plants make glucose", status: done ? "active" : "pending", early: true, mastery: 0.3 }] });
    const widget = { id: "O1.c1", number: 1, type: "mcq", widget: "choice", prompt: "Why do plants need sunlight?", choices: ["warmth", "energy to make food"],
      kind: "mcq", label: "Quick check", purpose: "check", objective_id: "O1", ask_confidence: false };
    let started: Record<string, unknown> | null = null;
    let answered: Record<string, unknown> | null = null;
    server.use(
      http.get(`${API}/chat/config`, () => HttpResponse.json({ collections: ["reference"], filter_fields: ["grade"], default_mode: "scoped", scope_mode: "prefill", scope_defaults: {} })),
      http.get(`${API}/chat/conversations`, () => HttpResponse.json({ items: [] })),
      http.post(`${API}/chat/conversations`, () => HttpResponse.json(conv)),
      http.get(`${API}/chat/conversations/c2`, () => HttpResponse.json({ conversation: conv, messages: [], artifacts: [] })),
      http.get(`${API}/chat/conversations/c2/learning-session`, () => HttpResponse.json({ session: null })),
      http.post(`${API}/chat/conversations/c2/learning-sessions`, async ({ request }) => {
        started = (await request.json()) as Record<string, unknown>;
        return sse([["intent", { skill: "learn" }], ["token", "Let's check what you know:"], ["learn_widget", widget], ["learn_state", state("active", false)], ["done", { title: "photosynthesis" }]]);
      }),
      http.post(`${API}/chat/conversations/c2/learn`, async ({ request }) => {
        answered = (await request.json()) as Record<string, unknown>;
        return sse([["learn_feedback", { widget_id: "O1.c1", correct: true, score: 1, expected: "energy to make food", explanation: "Light is energy." }],
          ["token", "Spot on! Next: how plants make glucose."], ["learn_state", state("mastered", true)], ["done", {}]]);
      }),
    );
    renderAt(<ChatPage />, { path: "/chat", pattern: "/chat/:conversationId?" });
    await userEvent.click(await screen.findByRole("button", { name: /Learn mode/ }));
    await userEvent.type(screen.getByRole("textbox", { name: "Message" }), "photosynthesis{Enter}");
    // the progress path and the inline check appear; the answer is not in the page
    expect(await screen.findByLabelText("Lesson: photosynthesis")).toBeInTheDocument();
    expect(await screen.findByText("Why do plants need sunlight?")).toBeInTheDocument();
    expect(started).toMatchObject({ topic: "photosynthesis" });
    expect(screen.getByRole("button", { name: "Hint" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /energy to make food/ }));
    await waitFor(() => expect(answered).toMatchObject({ widget_response: { widget_id: "O1.c1", response: 1 } }));
    // graded: the check collapses to a compact result; the tap reads as a receipt, not a bubble
    expect(await screen.findByText("Correct")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /warmth/ })).not.toBeInTheDocument();
    expect(screen.getByText("Your answer")).toBeInTheDocument();
    expect(screen.getByText("energy to make food")).toBeInTheDocument();
    expect(await screen.findByText(/Spot on!/)).toBeInTheDocument();
    expect(screen.getByText("1 of 2 done")).toBeInTheDocument();
    // the speaker is named once, where the lesson starts — not on every tutor turn
    expect(screen.getAllByText("Lesson")).toHaveLength(1);
  });
});
