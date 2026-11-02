import { useInfiniteQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useApi, type CopiedDocument, type DocumentKind, type DocumentView, type Page, type StoredDocument, type Visibility } from "@/lib/api";

export type Scope = "mine" | "school";

export function useDocuments(scope: Scope, kind: DocumentKind | null, q: string) {
  const api = useApi();
  return useInfiniteQuery({
    queryKey: ["documents", "list", scope, kind, scope === "school" ? q : ""],
    initialPageParam: null as string | null,
    queryFn: ({ pageParam, signal }) => api.get<Page<StoredDocument>>("/documents",
      { scope, kind, limit: 25, cursor: pageParam, q: scope === "school" ? q || null : null }, signal),
    getNextPageParam: (last) => last.next_cursor ?? null,
  });
}

export function useDocumentActions() {
  const api = useApi();
  const qc = useQueryClient();
  const refresh = () => qc.invalidateQueries({ queryKey: ["documents"] });
  const id = (d: StoredDocument) => encodeURIComponent(d.id);
  return {
    share: useMutation({
      mutationFn: ({ doc, visibility }: { doc: StoredDocument; visibility: Visibility }) => api.request<StoredDocument>("PATCH", `/documents/${id(doc)}`, { body: { visibility } }),
      onSuccess: refresh,
    }),
    copy: useMutation({ mutationFn: (doc: StoredDocument) => api.post<CopiedDocument>(`/documents/${id(doc)}/copy`, {}), onSuccess: refresh }),
    remove: useMutation({ mutationFn: (doc: StoredDocument) => api.request("DELETE", `/documents/${id(doc)}`), onSuccess: refresh }),
    exportZip: useMutation({
      mutationFn: async (doc: StoredDocument) => {
        if (doc.kind === "unit") return api.download("POST", `/units/${id(doc)}/export`, { fallbackName: `unit-${doc.id}.zip` });
        const v = await api.get<DocumentView>(`/documents/${id(doc)}`);
        return api.download("POST", "/lessons/export/bundle/zip", { body: v.ldd, fallbackName: "lesson.zip" });
      },
    }),
  };
}
