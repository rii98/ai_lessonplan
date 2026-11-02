import { screen, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import { Perm } from "@/features/session";
import { renderAt } from "@/test/render";
import { API, server, student } from "@/test/server";
import { RequirePerm } from "./guards";

describe("RequirePerm", () => {
  it("sends a student who opens the editor to chat", async () => {
    server.use(http.get(`${API}/me`, () => HttpResponse.json(student)));
    const { router } = renderAt(<RequirePerm perm={Perm.author}><div>editor</div></RequirePerm>, { extra: [{ path: "/chat", element: <div>chat</div> }] });
    await waitFor(() => expect(router.state.location.pathname).toBe("/chat"));
    expect(screen.queryByText("editor")).not.toBeInTheDocument();
  });

  it("renders for a teacher", async () => {
    renderAt(<RequirePerm perm={Perm.author}><div>editor</div></RequirePerm>);
    expect(await screen.findByText("editor")).toBeInTheDocument();
  });
});
