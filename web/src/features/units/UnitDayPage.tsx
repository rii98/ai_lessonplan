import { useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { useParams } from "react-router-dom";
import { LessonEditor, normalizeLesson, type LessonSource } from "@/features/lessons";
import { useMe } from "@/features/session";
import { describeError, useApi, type SavedUnit } from "@/lib/api";
import { ErrorState, Page, SkeletonLines } from "@/ui";
import { unitKeys, useUnit } from "./api";
import { CoherenceBanner } from "./UnitPage";

/** `/units/:unitId/days/:day` — the lesson editor bound to one day of a unit. */
export function UnitDayPage() {
  const { unitId = "", day: dayStr = "1" } = useParams();
  const day = Number(dayStr);
  const api = useApi();
  const qc = useQueryClient();
  const me = useMe().data;
  const q = useUnit(unitId);
  const [generation, setGeneration] = useState(0);
  const ldd = q.data?.unit.days[day - 1];
  const initial = useMemo(() => (ldd ? normalizeLesson(ldd) : null), [ldd]);

  if (q.isPending) return <Page width="narrow"><SkeletonLines lines={8} /></Page>;
  if (q.error || !q.data) return <Page width="narrow"><ErrorState title="Couldn't open this unit" error={describeError(q.error)} onRetry={() => q.refetch()} /></Page>;
  if (!initial) return <Page width="narrow"><ErrorState title={`Day ${day} doesn't exist`} error="This unit has fewer days." /></Page>;

  const doc = q.data.document;
  const source: LessonSource = {
    key: `unit:${unitId}:day:${day}`,
    baseVersion: doc.head_version_id ?? null,
    documentId: null, // history belongs to the whole unit
    readOnly: me?.id !== doc.owner_id,
    crumbs: [{ label: `${q.data.unit.title} · Day ${day}`, to: `/units/${unitId}` }],
    studioQuery: `unit=${encodeURIComponent(unitId)}&day=${day}`,
    async save(lesson) {
      const saved = await api.request<SavedUnit>("PUT", `/units/${encodeURIComponent(unitId)}/days/${day}`, { body: lesson });
      qc.setQueryData(unitKeys.unit(unitId), { document: saved.document, unit: saved.unit });
      // a day edit can drift the arc — show the recomputed cross-day coherence right away
      return { versionId: saved.version_id, notice: <CoherenceBanner report={saved.unit.coherence} compact /> };
    },
    reload: async () => { await q.refetch(); setGeneration((g) => g + 1); },
  };
  return <LessonEditor key={`${unitId}:${day}:${generation}`} initial={initial} source={source} />;
}
