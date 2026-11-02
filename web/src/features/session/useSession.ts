import { useQuery } from "@tanstack/react-query";
import { useApi } from "@/lib/api";
import type { Me, PermName } from "./types";

export const sessionKeys = { me: ["session", "me"] as const };

/** Who is signed in and what they may do. Cached for the session. */
export function useMe() {
  const api = useApi();
  return useQuery({
    queryKey: sessionKeys.me,
    queryFn: ({ signal }) => api.get<Me>("/me", undefined, signal),
    staleTime: 5 * 60_000,
  });
}

export function useCan(perm: PermName): boolean {
  const { data } = useMe();
  return !!data?.permissions.includes(perm);
}

export const canIn = (me: Me | undefined, perm: PermName) => !!me?.permissions.includes(perm);
