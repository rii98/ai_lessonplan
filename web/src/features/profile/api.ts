import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useApi, type TeacherProfile } from "@/lib/api";

export const profileKeys = { me: ["profile"] as const };

/** The teacher's saved defaults (already pre-filled from their Edboox context). */
export function useProfile() {
  const api = useApi();
  return useQuery({
    queryKey: profileKeys.me,
    queryFn: ({ signal }) => api.get<TeacherProfile>("/profile", undefined, signal),
    staleTime: 5 * 60_000,
  });
}

export function useSaveProfile() {
  const api = useApi();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (p: TeacherProfile) => api.request<TeacherProfile>("PUT", "/profile", { body: p }),
    onSuccess: (p) => qc.setQueryData(profileKeys.me, p),
  });
}
