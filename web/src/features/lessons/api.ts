import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  useApi, type Critique, type DocumentVersionInfo, type DocumentView, type LessonDesignDocument, type Page,
  type RefineResult, type SavedLesson, type SaveLessonRequest, type StoredDocument, type ValidationResult,
} from "@/lib/api";
import { useDebouncedValue } from "@/lib/hooks";
import { toIssues, type Issue } from "./model/issues";
import type { Lesson } from "./model/ldd";

export const lessonKeys = {
  all: ["lessons"] as const,
  document: (id: string) => ["documents", id] as const,
  versions: (id: string) => ["documents", id, "versions"] as const,
  documents: ["documents"] as const,
};

export function useDocumentView(id: string) {
  const api = useApi();
  return useQuery({
    queryKey: lessonKeys.document(id),
    queryFn: ({ signal }) => api.get<DocumentView>(`/documents/${encodeURIComponent(id)}`, undefined, signal),
  });
}

/** Generate a lesson through the full pipeline AND save it as a new document. */
export function useCreateLesson() {
  const api = useApi();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (req: SaveLessonRequest) => api.post<SavedLesson>("/documents/lessons", req),
    onSuccess: (saved) => {
      qc.setQueryData(lessonKeys.document(saved.document.id), { document: saved.document, ldd: saved.ldd } satisfies DocumentView);
      void qc.invalidateQueries({ queryKey: lessonKeys.documents, exact: true });
    },
  });
}

export function useVersions(id: string | null) {
  const api = useApi();
  return useQuery({
    queryKey: lessonKeys.versions(id ?? "-"),
    enabled: !!id,
    queryFn: ({ signal }) => api.get<Page<DocumentVersionInfo>>(`/documents/${encodeURIComponent(id!)}/versions`, undefined, signal),
    select: (p) => p.items,
  });
}

/** Move the document head (server-side history). */
export function useMoveHead(id: string) {
  const api = useApi();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (dir: "undo" | "redo") => api.post<StoredDocument>(`/documents/${encodeURIComponent(id)}/${dir}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: lessonKeys.document(id) }),
  });
}

export interface LiveValidation {
  issues: Issue[];
  valid: boolean | null;
  checking: boolean;
  /** Revision the result belongs to — stale when it trails the draft. */
  revision: number;
}

/**
 * Guardrails as you type: validate the draft (no LLM, ~ms) once edits pause.
 * HTTP 200 even when invalid, so problems arrive as data, not errors.
 */
export function useLiveValidation(lesson: Lesson, revision: number, scope: string): LiveValidation {
  const api = useApi();
  const settled = useDebouncedValue(revision, 700);
  const q = useQuery({
    queryKey: ["lessons", "validate", scope, settled],
    // the closure reads the latest lesson; the key changes only when edits settle
    queryFn: async ({ signal }) => ({ rev: settled, res: await api.request<ValidationResult>("POST", "/lessons/validate", { body: lesson, signal }) }),
    placeholderData: keepPreviousData,
    staleTime: Infinity,
    gcTime: 60_000,
    retry: 1,
  });
  const res = q.data?.res;
  return {
    issues: res ? toIssues(res.errors ?? []) : [],
    valid: res ? res.valid : null,
    checking: q.isFetching || settled !== revision,
    revision: q.data?.rev ?? -1,
  };
}

export function useRefineLesson() {
  const api = useApi();
  return useMutation({
    mutationFn: (req: { ldd: LessonDesignDocument; target: string; instruction: string }) =>
      api.post<RefineResult>("/lessons/refine", req),
  });
}

export function useCritique() {
  const api = useApi();
  return useMutation({ mutationFn: (ldd: LessonDesignDocument) => api.post<Critique>("/lessons/critique", ldd) });
}

export type ExportKind = "lesson_plan" | "slides" | "worksheet" | "quiz" | "bundle";

export function useExportLesson() {
  const api = useApi();
  return useMutation({
    mutationFn: ({ ldd, kind }: { ldd: LessonDesignDocument; kind: ExportKind }) =>
      kind === "bundle"
        ? api.download("POST", "/lessons/export/bundle/zip", { body: ldd, fallbackName: "lesson_bundle.zip" })
        : api.download("POST", `/lessons/export/${kind}`, { body: ldd, fallbackName: `${kind}.docx` }),
  });
}
