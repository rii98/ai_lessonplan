import { render } from "@testing-library/react";
import type { ReactNode } from "react";
import { createMemoryRouter, RouterProvider } from "react-router-dom";
import { createQueryClient, LessonForgeProvider } from "@/app/LessonForgeProvider";
import { createApiClient, type AuthStrategy } from "@/lib/api";

export const noAuth: AuthStrategy = { prepare: (init) => { init.headers.set("X-LF-Request", "1"); } };

/** Render `element` at `path` (matching `pattern`) inside the real providers, against MSW. */
export function renderAt(element: ReactNode, { path = "/", pattern = "/", extra = [] as { path: string; element: ReactNode }[] } = {}) {
  const router = createMemoryRouter([{ path: pattern, element }, ...extra, { path: "*", element: <div>elsewhere</div> }], { initialEntries: [path] });
  const queryClient = createQueryClient();
  queryClient.setDefaultOptions({ queries: { retry: false, staleTime: Infinity }, mutations: { retry: false } });
  const utils = render(
    <LessonForgeProvider config={{ apiBaseUrl: "http://localhost/api/v1", auth: noAuth, queryClient }}>
      <RouterProvider router={router} />
    </LessonForgeProvider>,
  );
  return { ...utils, router, queryClient };
}

export { createApiClient };
