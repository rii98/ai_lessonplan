import { createContext, useContext, type ReactNode } from "react";
import type { ApiClient } from "./client";

const ApiContext = createContext<ApiClient | null>(null);

export function ApiProvider({ client, children }: { client: ApiClient; children: ReactNode }) {
  return <ApiContext.Provider value={client}>{children}</ApiContext.Provider>;
}

/** The API client of the nearest `LessonForgeProvider`. */
export function useApi(): ApiClient {
  const c = useContext(ApiContext);
  if (!c) throw new Error("useApi() must be used inside <LessonForgeProvider>");
  return c;
}
