import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useMe } from "@/features/session";
import { describeError, useApi, type CopiedDocument, type SavedLesson } from "@/lib/api";
import { Banner, Button, ErrorState, Icon, Page, SkeletonLines } from "@/ui";
import { lessonKeys, useDocumentView } from "../api";
import { LessonEditor, type LessonSource } from "../editor/LessonEditor";
import { normalizeLesson } from "../model/ldd";

/** `/lessons/:documentId` — a saved lesson. Colleagues' shared plans open read-only. */
export function LessonPage() {
  const { documentId = "" } = useParams();
  const api = useApi();
  const qc = useQueryClient();
  const navigate = useNavigate();
  const me = useMe().data;
  const view = useDocumentView(documentId);
  const [generation, setGeneration] = useState(0);

  const copy = useMutation({
    mutationFn: () => api.post<CopiedDocument>(`/documents/${encodeURIComponent(documentId)}/copy`, {}),
    onSuccess: (c) => navigate(`/lessons/${c.document.id}`),
  });

  const doc = view.data?.document;
  const initial = useMemo(() => (view.data ? normalizeLesson(view.data.ldd) : null), [view.data]);

  if (view.isPending) return <Page width="narrow"><SkeletonLines lines={8} /></Page>;
  if (view.error || !doc || !initial) {
    return <Page width="narrow"><ErrorState title="Couldn't open this lesson" error={describeError(view.error)} onRetry={() => view.refetch()} /></Page>;
  }

  const mine = !!me && doc.owner_id === me.id;
  const source: LessonSource = {
    key: `doc:${doc.id}`,
    baseVersion: doc.head_version_id ?? null,
    documentId: mine ? doc.id : null,
    readOnly: !mine,
    crumbs: [{ label: "Library", to: "/library" }],
    studioQuery: `lesson=${encodeURIComponent(doc.id)}`,
    readOnlyNotice: (
      <Banner tone="info" title={`Shared by ${doc.owner_name || "a colleague"}`}
        actions={<Button size="sm" variant="primary" busy={copy.isPending} icon={<Icon.Copy size={15} />} onClick={() => copy.mutate()}>Copy to my work</Button>}>
        Make your own copy to adapt it — the original stays theirs.
        {copy.error && <div style={{ color: "var(--brick)" }}>{describeError(copy.error)}</div>}
      </Banner>
    ),
    async save(lesson) {
      const saved = await api.post<SavedLesson>(`/documents/${encodeURIComponent(doc.id)}/manual`, lesson);
      // keep the cache current for the next visit; the open editor isn't reset by it
      qc.setQueryData(lessonKeys.document(doc.id), { document: saved.document, ldd: saved.ldd });
      void qc.invalidateQueries({ queryKey: lessonKeys.versions(doc.id) });
      void qc.invalidateQueries({ queryKey: lessonKeys.documents, exact: true });
      return { versionId: saved.version_id };
    },
    reload: async () => { await view.refetch(); setGeneration((g) => g + 1); },
  };

  return <LessonEditor key={`${doc.id}:${generation}`} initial={initial} source={source} />;
}
