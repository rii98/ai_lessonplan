import { http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";
import { server } from "@/test/server";
import { bearerToken, cookieSession } from "./auth";
import { createApiClient } from "./client";
import { ApiError, describeError } from "./errors";

const BASE = "http://localhost/api/v1";

describe("api client", () => {
  it("sends the CSRF header with the cookie session and parses JSON", async () => {
    let seen: Headers | null = null;
    server.use(http.post(`${BASE}/thing`, ({ request }) => { seen = request.headers; return HttpResponse.json({ ok: true }); }));
    const api = createApiClient({ baseUrl: BASE, auth: cookieSession({ redirect: vi.fn() }) });
    await expect(api.post("/thing", { a: 1 })).resolves.toEqual({ ok: true });
    expect(seen!.get("x-lf-request")).toBe("1");
    expect(seen!.get("content-type")).toBe("application/json");
  });

  it("turns the error envelope into an ApiError with field issues", async () => {
    server.use(http.post(`${BASE}/bad`, () => HttpResponse.json(
      { error: { code: "validation", message: "invalid lesson", details: [{ loc: "phases.0.minutes", msg: "must be > 0" }], request_id: "r1" } }, { status: 422 })));
    const api = createApiClient({ baseUrl: BASE, auth: cookieSession({ redirect: vi.fn() }) });
    const err = await api.post("/bad").catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err).toMatchObject({ status: 422, code: "validation", requestId: "r1" });
    expect(describeError(err)).toBe("invalid lesson: phases.0.minutes — must be > 0");
  });

  it("sends a 401 to the server-provided login URL exactly once", async () => {
    server.use(http.get(`${BASE}/me`, () => HttpResponse.json({ error: { code: "auth.missing", message: "x", details: { login_url: "https://edboox/login" } } }, { status: 401 })));
    const redirect = vi.fn();
    const api = createApiClient({ baseUrl: BASE, auth: cookieSession({ redirect }) });
    await api.get("/me").catch(() => {});
    await api.get("/me").catch(() => {});
    expect(redirect).toHaveBeenCalledTimes(1);
    expect(redirect).toHaveBeenCalledWith("https://edboox/login");
  });

  it("supports a bearer token strategy (embedding in Edboox)", async () => {
    let auth: string | null = null;
    server.use(http.get(`${BASE}/me`, ({ request }) => { auth = request.headers.get("authorization"); return HttpResponse.json({}); }));
    await createApiClient({ baseUrl: BASE, auth: bearerToken(() => "tok") }).get("/me");
    expect(auth).toBe("Bearer tok");
  });

  it("drops empty query params", async () => {
    let url = "";
    server.use(http.get(`${BASE}/documents`, ({ request }) => { url = request.url; return HttpResponse.json({ items: [] }); }));
    await createApiClient({ baseUrl: BASE, auth: bearerToken(() => null) }).get("/documents", { scope: "mine", kind: null, q: "" });
    expect(new URL(url).search).toBe("?scope=mine");
  });

  it("streams SSE frames, even when split across chunks", async () => {
    const enc = new TextEncoder();
    server.use(http.post(`${BASE}/stream`, () => new HttpResponse(new ReadableStream({
      start(c) {
        c.enqueue(enc.encode('event: token\ndata: "Hel'));
        c.enqueue(enc.encode('lo"\n\nevent: done\ndata: {"title":"T"}\n\n'));
        c.close();
      },
    }), { headers: { "content-type": "text/event-stream" } })));
    const events = [];
    for await (const ev of createApiClient({ baseUrl: BASE, auth: bearerToken(() => null) }).stream("/stream", {})) events.push(ev);
    expect(events).toEqual([{ event: "token", data: "Hello" }, { event: "done", data: { title: "T" } }]);
  });
});
