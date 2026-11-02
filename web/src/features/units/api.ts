import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  useApi, type GenerateUnitRequest, type Page, type PlanResponse, type SavedUnit, type StoredDocument, type UnitRequest, type UnitView,
} from "@/lib/api";

export const unitKeys = {
  unit: (id: string) => ["units", id] as const,
  list: ["documents", "list", "unit"] as const,
};

export function useUnit(id: string) {
  const api = useApi();
  return useQuery({
    queryKey: unitKeys.unit(id),
    queryFn: ({ signal }) => api.get<UnitView>(`/units/${encodeURIComponent(id)}`, undefined, signal),
  });
}

export function useUnitList() {
  const api = useApi();
  return useQuery({
    queryKey: unitKeys.list,
    queryFn: ({ signal }) => api.get<Page<StoredDocument>>("/documents", { scope: "mine", kind: "unit", limit: 50 }, signal),
    select: (p) => p.items,
  });
}

/** One cheap planner call: the arc, plus the chapter's syllabus outline for coverage. */
export function usePlanUnit() {
  const api = useApi();
  return useMutation({ mutationFn: (req: UnitRequest) => api.post<PlanResponse>("/units/plan", req) });
}

export function useGenerateUnit() {
  const api = useApi();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (req: GenerateUnitRequest) => api.post<SavedUnit>("/units/generate", req),
    onSuccess: (saved) => {
      qc.setQueryData(unitKeys.unit(saved.document.id), { document: saved.document, unit: saved.unit } satisfies UnitView);
      void qc.invalidateQueries({ queryKey: ["documents"] });
    },
  });
}

export function useRegenerateDay(unitId: string) {
  const api = useApi();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (day: number) => api.post<SavedUnit>(`/units/${encodeURIComponent(unitId)}/days/${day}/regenerate`),
    onSuccess: (saved) => qc.setQueryData(unitKeys.unit(unitId), { document: saved.document, unit: saved.unit } satisfies UnitView),
  });
}

export function useExportUnit(unitId: string) {
  const api = useApi();
  return useMutation({
    mutationFn: () => api.download("POST", `/units/${encodeURIComponent(unitId)}/export`, { fallbackName: `unit-${unitId}.zip` }),
  });
}
