import { useQuery } from "@tanstack/react-query";
import { useApi, type AssessmentSpec } from "@/lib/api";
import { useDebouncedValue } from "@/lib/hooks";
import type { QuestionCatalog, RetrievalPlan } from "./types";

export function useQuestionCatalog() {
  const api = useApi();
  return useQuery({
    queryKey: ["artifacts", "question-types"],
    staleTime: Infinity,
    queryFn: ({ signal }) => api.get<QuestionCatalog>("/artifacts/question-types", undefined, signal),
  });
}

export function useArtifactFormats() {
  const api = useApi();
  return useQuery({
    queryKey: ["artifacts", "manifest"],
    staleTime: Infinity,
    queryFn: ({ signal }) => api.get<{ generatable: { kind: string; formats: string[] }[] }>("/artifacts/manifest", undefined, signal),
    select: (m) => Object.fromEntries(m.generatable.map((g) => [g.kind, g.formats])) as Record<string, string[]>,
  });
}

/** How a blueprint will be grounded — free (no LLM), previewed while you edit. */
export function useRetrievalPlan(spec: AssessmentSpec | null) {
  const api = useApi();
  const settled = useDebouncedValue(spec, 350);
  return useQuery({
    queryKey: ["artifacts", "retrieval-plan", settled],
    enabled: !!settled,
    staleTime: Infinity,
    queryFn: ({ signal }) => api.request<RetrievalPlan>("POST", "/artifacts/retrieval-plan", { body: settled, signal }),
  });
}
