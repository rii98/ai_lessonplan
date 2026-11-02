import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState, type ReactNode } from "react";
import { ApiError, ApiProvider, cookieSession, createApiClient, type AuthStrategy } from "@/lib/api";
import { ToastProvider } from "@/ui";

export interface LessonForgeConfig {
  /** Where the versioned API lives. Default: same-origin `/api/v1`. */
  apiBaseUrl?: string;
  /** How requests authenticate. Default: the cookie session from the Edboox launch. */
  auth?: AuthStrategy;
  /** Bring your own cache (e.g. to share it with a host app). */
  queryClient?: QueryClient;
}

export function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 30_000,
        // Teachers are often on metered data: don't refetch just because a tab regained focus.
        refetchOnWindowFocus: false,
        retry: (count, err) => {
          if (err instanceof ApiError && err.status !== 0 && err.status < 500) return false;
          return count < 2;
        },
      },
      mutations: { retry: false },
    },
  });
}

/**
 * Everything a LessonForge screen needs: the API client, the query cache and
 * toasts. The standalone app wraps itself in this; a host (Edboox) can wrap any
 * feature page in it with its own `apiBaseUrl` and `auth`.
 */
export function LessonForgeProvider({ config = {}, children }: { config?: LessonForgeConfig; children: ReactNode }) {
  const [client] = useState(() => createApiClient({ baseUrl: config.apiBaseUrl ?? "/api/v1", auth: config.auth ?? cookieSession() }));
  const [queryClient] = useState(() => config.queryClient ?? createQueryClient());
  return (
    <ApiProvider client={client}>
      <QueryClientProvider client={queryClient}>
        <ToastProvider>{children}</ToastProvider>
      </QueryClientProvider>
    </ApiProvider>
  );
}
