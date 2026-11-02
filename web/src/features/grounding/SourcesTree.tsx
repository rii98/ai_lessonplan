import { useQuery } from "@tanstack/react-query";
import { describeError, useApi } from "@/lib/api";
import { breadcrumb, plural } from "@/lib/format";
import { Badge, ErrorState, Muted, SkeletonLines } from "@/ui";
import type { GroundingQuery, GroundingTree } from "./types";
import s from "./grounding.module.css";

export function useGrounding(q: GroundingQuery | null) {
  const api = useApi();
  return useQuery({
    queryKey: ["grounding", q],
    enabled: !!q?.topic?.trim(),
    staleTime: 5 * 60_000,
    queryFn: ({ signal }) => api.request<GroundingTree>("POST", "/grounding/preview", { body: q, signal }),
  });
}

/**
 * What the AI grounded on: collection → document → the retrieved passages
 * (expand to read one). The same lookup the generator runs, done live.
 */
export function SourcesTree({ query }: { query: GroundingQuery | null }) {
  const { data, isPending, error, refetch } = useGrounding(query);
  if (!query?.topic?.trim()) return <Muted>Add a topic to see which reference material it draws on.</Muted>;
  if (isPending) return <SkeletonLines lines={4} />;
  if (error) return <ErrorState error={describeError(error)} onRetry={() => refetch()} title="Couldn't look up sources" />;
  if (!data.grounded) return <Muted>No reference material matched this topic — the lesson comes from the model's own curriculum knowledge. Adding the chapter to the knowledge base will ground it.</Muted>;
  return (
    <div className={s.tree}>
      {data.collections.map((c) => (
        <div key={c.name} className={s.collection}>
          <div className={s.colHead}>
            <Badge tone={c.authoritative ? "pine" : "neutral"}>{c.name.replace("_", " ")}</Badge>
            {c.authoritative && <span className={s.meta}>authoritative</span>}
            <span className={s.meta}>{plural(c.count, "passage")}</span>
          </div>
          {c.documents.map((d) => (
            <div key={d.source} className={s.doc}>
              <div className={s.docTitle}>{d.source}</div>
              {d.chunks.map((ch, i) => (
                <details key={i} className={s.chunk}>
                  <summary>
                    <span>{breadcrumb(ch.heading_path) || breadcrumb(ch.chapter) || `Passage ${(ch.chunk_index ?? i) + 1}`}</span>
                    <span className={s.score}>{ch.score.toFixed(2)}</span>
                  </summary>
                  <div className={s.text}>{ch.text || "(no text)"}</div>
                </details>
              ))}
            </div>
          ))}
        </div>
      ))}
    </div>
  );
}
