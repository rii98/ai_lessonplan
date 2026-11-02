import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useApi } from "@/lib/api";

// contract gaps — api/routers/corpus.py returns untyped dicts
export interface Space { id: string; name: string; writable: boolean }
export interface CollectionInfo { name: string; description: string; count: number }
export interface CorpusRecord { id: string; text: string; source?: string | null; metadata?: Record<string, unknown> }
export interface ChunkPreview { total: number; truncated: boolean; chunks: { heading_path?: string; heading?: string; chars: number; text: string }[] }
export interface DocumentIngest {
  collection: string; text: string; format: "markdown"; source: string | null; chunker: string | null;
  metadata: Record<string, unknown>; params: Record<string, unknown>; space: string;
}

const k = {
  spaces: ["corpus", "spaces"] as const,
  overview: (space: string) => ["corpus", "overview", space] as const,
  records: (space: string, c: string) => ["corpus", "records", space, c] as const,
};

export function useSpaces() {
  const api = useApi();
  return useQuery({ queryKey: k.spaces, queryFn: ({ signal }) => api.get<{ spaces: Space[] }>("/corpus/spaces", undefined, signal), select: (d) => d.spaces });
}

export function useOverview(space: string) {
  const api = useApi();
  return useQuery({ queryKey: k.overview(space), queryFn: ({ signal }) => api.get<{ collections: CollectionInfo[] }>("/corpus/overview", { space }, signal), select: (d) => d.collections });
}

export function useChunkers() {
  const api = useApi();
  return useQuery({ queryKey: ["corpus", "chunkers"], staleTime: Infinity, queryFn: ({ signal }) => api.get<{ chunkers: string[]; by_format?: Record<string, string> }>("/corpus/chunkers", undefined, signal) });
}

export function useRecords(space: string, collection: string | null) {
  const api = useApi();
  return useInfiniteQuery({
    queryKey: k.records(space, collection ?? "-"),
    enabled: !!collection,
    initialPageParam: null as string | null,
    queryFn: ({ pageParam, signal }) => api.get<{ items: CorpusRecord[]; next_cursor?: string | null }>(
      `/corpus/collections/${encodeURIComponent(collection!)}/records`, { limit: 25, space, cursor: pageParam }, signal),
    getNextPageParam: (p) => p.next_cursor ?? null,
  });
}

export function useCorpusMutations(space: string) {
  const api = useApi();
  const qc = useQueryClient();
  const refresh = () => qc.invalidateQueries({ queryKey: ["corpus"] });
  return {
    ingest: useMutation({
      mutationFn: (b: { collection: string; records: Record<string, unknown>[] }) => api.post<{ documents: number; chunks: number }>("/corpus/ingest", { ...b, space }),
      onSuccess: refresh,
    }),
    ingestDocument: useMutation({ mutationFn: (b: DocumentIngest) => api.post<{ chunks: number }>("/corpus/ingest/document", b), onSuccess: refresh }),
    preview: useMutation({ mutationFn: (b: DocumentIngest) => api.post<ChunkPreview>("/corpus/preview", b) }),
    remove: useMutation({
      mutationFn: ({ collection, id }: { collection: string; id: string }) => api.post(`/corpus/collections/${encodeURIComponent(collection)}/delete`, { ids: [id] }),
      onSuccess: refresh,
    }),
  };
}

/** One JSON object per line, each with non-empty `text`. Throws a line-numbered error. */
export function parseJsonl(text: string): Record<string, unknown>[] {
  const out: Record<string, unknown>[] = [];
  text.split("\n").forEach((raw, i) => {
    const line = raw.trim();
    if (!line || line.startsWith("//")) return;
    let obj: unknown;
    try { obj = JSON.parse(line); } catch { throw new Error(`Line ${i + 1} isn't valid JSON.`); }
    if (!obj || typeof obj !== "object" || Array.isArray(obj)) throw new Error(`Line ${i + 1} must be a JSON object.`);
    if (!String((obj as { text?: unknown }).text ?? "").trim()) throw new Error(`Line ${i + 1} needs a non-empty "text".`);
    out.push(obj as Record<string, unknown>);
  });
  if (!out.length) throw new Error("No records found — paste at least one JSON line.");
  return out;
}
