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
});
