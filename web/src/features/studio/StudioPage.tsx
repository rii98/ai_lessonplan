import { useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { Link, useLocation, useParams, useSearchParams } from "react-router-dom";
import { ReviseDialog, type Proposal } from "@/features/lessons";
import { describeError, useApi, type AssessmentSpec, type DocumentView, type LessonDesignDocument, type UnitView } from "@/lib/api";
import { safeStorage, useDraft, useElapsed, useHotkey, useStoredState } from "@/lib/hooks";
import {
  Banner, Button, ErrorState, Icon, Page, PageHeader, ProgressPanel, Section, Select, SkeletonLines, useToast,
} from "@/ui";
import { useArtifactFormats, useQuestionCatalog } from "./api";
import { Blueprint, ordered, specProblem } from "./Blueprint";
import { isStudioKind, STUDIO_KINDS } from "./kinds";
import type { Artifact, StudioHandoff, StudioKind, StudioSource } from "./types";
import s from "./studio.module.css";

function sourceFrom(params: URLSearchParams, state: unknown): StudioSource | null {
  const lesson = params.get("lesson"), unit = params.get("unit"), day = params.get("day");
  if (lesson) return { type: "lesson", documentId: lesson };
  if (unit && day) return { type: "unitDay", unitId: unit, day: Number(day) };
  if (unit) return { type: "unit", unitId: unit };
  const brief = (state as StudioHandoff | null)?.brief;
  return brief ? { type: "brief", brief } : null;
}

interface StoredArtifact { kind: StudioKind; sourceKey: string; artifact: Artifact }

/**
 * `/studio/:kind` — a quiz, worksheet or deck from a brief, a lesson, a unit day
 * or a whole unit: blueprint → generate → edit (by hand or with AI) → export.
 * Studio artifacts aren't stored on the server, so the draft is kept on this
 * device until it's downloaded.
 */
export function StudioPage() {
  const { kind: kindParam } = useParams();
  const [params] = useSearchParams();
  const location = useLocation();
  const source = useMemo(() => sourceFrom(params, location.state), [params, location.state]);
  if (!isStudioKind(kindParam)) return <Page width="narrow"><ErrorState title="Unknown kind" error="The studio makes quizzes, worksheets and slides." /></Page>;
  if (!source) {
    return (
      <Page width="narrow">
        <Banner tone="info" title="Start from a brief, a lesson or a unit" actions={<Link to="/"><Button size="sm" variant="primary">Go to Plan</Button></Link>}>
          Describe the topic on the Plan page, or open a lesson or unit and choose “Build from this”.
        </Banner>
      </Page>
    );
  }
  return <Studio key={`${kindParam}:${JSON.stringify(source)}`} kind={kindParam} source={source} />;
}

/** The lesson(s) the assessment is tied to, when it comes from saved work. */
function useSourceContext(source: StudioSource) {
  const api = useApi();
  return useQuery({
    queryKey: ["studio", "source", source],
    enabled: source.type !== "brief",
    queryFn: async ({ signal }): Promise<{ title: string; ldd?: LessonDesignDocument; days?: { n: number; topic: string }[] }> => {
      if (source.type === "brief") return { title: source.brief.topic ?? "your brief" };
      if (source.type === "lesson") {
        const v = await api.get<DocumentView>(`/documents/${encodeURIComponent(source.documentId)}`, undefined, signal);
        return { title: v.document.title || v.ldd.topic, ldd: v.ldd };
      }
      const v = await api.get<UnitView>(`/units/${encodeURIComponent(source.unitId)}`, undefined, signal);
      if (source.type === "unitDay") {
        const ldd = v.unit.days[source.day - 1];
        if (!ldd) throw new Error(`Day ${source.day} doesn't exist.`);
        return { title: `${v.unit.title} · Day ${source.day}`, ldd };
      }
      return { title: v.unit.title, days: v.unit.days.map((d, i) => ({ n: i + 1, topic: d.topic })) };
    },
  });
}

function Studio({ kind, source }: { kind: StudioKind; source: StudioSource }) {
  const api = useApi();
  const toast = useToast();
  const meta = STUDIO_KINDS[kind];
  const ctx = useSourceContext(source);
  const catalog = useQuestionCatalog();
  const sourceKey = JSON.stringify(source);
  const storeKey = `lf.studio.${kind}`;
  const [savedSpec, saveSpec] = useStoredState<AssessmentSpec | null>("lf.assessmentSpec", null);
  const [spec, setSpec] = useState<AssessmentSpec | null>(null);
  const [days, setDays] = useState<number[]>([]);
  const [artifact, setArtifact] = useState<Artifact | null>(() => {
    const st = safeStorage.get<StoredArtifact>(storeKey);
    return st && st.sourceKey === sourceKey ? st.artifact : null;
  });
  const [generating, setGenerating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const elapsed = useElapsed(generating);

  // starting blueprint: what this teacher used last, else the first preset
  useEffect(() => {
    if (spec || !catalog.data) return;
    const types = savedSpec?.types?.length ? savedSpec.types : catalog.data.presets[0]?.types ?? [];
    setSpec({ scope: source.type === "unit" ? "chapter" : "auto", explanations: true, types: types.map((t) => ({ ...t, instruction: t.instruction ?? "" })) });
  }, [catalog.data, savedSpec, spec, source.type]);
  useEffect(() => { if (ctx.data?.days && !days.length) setDays(ctx.data.days.map((d) => d.n)); }, [ctx.data, days.length]);

  const generate = async () => {
    setError(null);
    const body = meta.blueprint && spec && catalog.data ? ordered(spec, catalog.data) : null;
    if (body) saveSpec({ ...body, scope: "auto" });
    setGenerating(true);
    try {
      let out: Artifact;
      if (source.type === "brief") out = await api.post<Artifact>(`/artifacts/${kind}/generate`, { ...source.brief, spec: body });
      else if (source.type === "unit") out = await api.post<Artifact>(`/units/${encodeURIComponent(source.unitId)}/assessment/${kind}`, { spec: body, days });
      else out = await api.post<Artifact>(`/artifacts/${kind}/from-lesson`, { ldd: ctx.data!.ldd, spec: body });
      setArtifact(out);
      safeStorage.set(storeKey, { kind, sourceKey, artifact: out } satisfies StoredArtifact);
    } catch (e) {
      setError(describeError(e));
    } finally {
      setGenerating(false);
    }
  };

  // slides have no blueprint: start straight away from a brief/lesson
  const auto = !meta.blueprint && !artifact && !generating && !error && (source.type === "brief" || !!ctx.data);
  useEffect(() => { if (auto) void generate(); });

  const from = source.type === "brief" ? (source.brief.topic ?? "your brief") : ctx.data?.title;
  const blocked = (meta.blueprint && (!spec || !catalog.data || !!specProblem(spec, catalog.data))) || (source.type === "unit" && !days.length) || (source.type !== "brief" && !ctx.data);

  return (
    <Page>
      <PageHeader eyebrow={<Link to={backHref(source)}><Icon.ArrowLeft size={13} /> Back</Link>} title={`${artifact ? "Edit" : "Build"} a ${meta.noun}`}
        lede={artifact ? "Edit anything, or ask the AI to revise a part. Download when it's ready." : undefined} />

      <div className={s.source}>
        <span className={s.sourceIcon}><Icon.BookOpen size={18} /></span>
        <div style={{ minWidth: 0 }}>
          <div style={{ fontSize: "var(--text-xs)", color: "var(--ink-3)", textTransform: "uppercase", letterSpacing: "0.06em", fontWeight: 650 }}>
            {source.type === "brief" ? "From your brief" : source.type === "unit" ? "From the unit" : "From the lesson"}
          </div>
          <div style={{ fontWeight: 600 }}>{ctx.isPending && source.type !== "brief" ? "Loading…" : from}</div>
          {source.type !== "brief" && <div style={{ fontSize: "var(--text-xs)", color: "var(--ink-3)" }}>Every question is tied to {source.type === "unit" ? "the chosen days'" : "its"} objectives.</div>}
        </div>
        {artifact && <Button size="sm" variant="quiet" style={{ marginLeft: "auto" }} icon={<Icon.RefreshCw size={14} />}
          onClick={() => { setArtifact(null); safeStorage.remove(storeKey); }}>Start over</Button>}
      </div>
      {ctx.error && <ErrorState error={describeError(ctx.error)} onRetry={() => ctx.refetch()} />}

      {!artifact && (
        <>
          {meta.blueprint && (
            <Section title="Which questions?">
              {catalog.isPending || !spec ? <SkeletonLines lines={4} /> : catalog.error ? <ErrorState error={describeError(catalog.error)} onRetry={() => catalog.refetch()} /> : (
                <Blueprint spec={spec} onChange={setSpec} catalog={catalog.data}
                  days={ctx.data?.days ? { all: ctx.data.days, selected: days, onChange: setDays } : undefined} />
              )}
            </Section>
          )}
          {generating ? (
            <ProgressPanel title={`Writing the ${meta.noun}`} elapsed={elapsed} stages={[
              { label: "Grounding", detail: "looking up the textbook passages each question type needs" },
              { label: "Writing", detail: "drafting questions, answers and marking points" },
            ]} note="Usually 20–60 seconds." />
          ) : meta.blueprint && (
            <div style={{ display: "flex", gap: 8 }}><Button variant="primary" disabled={blocked} icon={<Icon.PenLine size={16} />} onClick={() => void generate()}>Write the {meta.noun}</Button></div>
          )}
          {error && <div style={{ marginTop: 12 }}><Banner tone="danger" title={`Couldn't write the ${meta.noun}`} actions={<Button size="sm" onClick={() => void generate()}>Try again</Button>}>{error}</Banner></div>}
        </>
      )}

      {artifact && <ArtifactWorkbench kind={kind} initial={artifact} onChange={(a) => safeStorage.set(storeKey, { kind, sourceKey, artifact: a } satisfies StoredArtifact)} toast={toast} />}
    </Page>
  );
}

const backHref = (src: StudioSource) =>
  src.type === "lesson" ? `/lessons/${src.documentId}` : src.type === "unitDay" ? `/units/${src.unitId}/days/${src.day}` : src.type === "unit" ? `/units/${src.unitId}` : "/";

function ArtifactWorkbench({ kind, initial, onChange, toast }: { kind: StudioKind; initial: Artifact; onChange: (a: Artifact) => void; toast: ReturnType<typeof useToast> }) {
  const api = useApi();
  const meta = STUDIO_KINDS[kind];
  const draft = useDraft<Artifact>(initial);
  const formats = useArtifactFormats().data?.[kind] ?? ["docx"];
  const [fmt, setFmt] = useState<string | null>(null);
  const [target, setTarget] = useState<string | null>(null);
  const [exporting, setExporting] = useState(false);
  const Editor = meta.Editor;
  const notes = "notes" in draft.value ? draft.value.notes : [];

  useEffect(() => { onChange(draft.value); }, [draft.value]);  // eslint-disable-line react-hooks/exhaustive-deps
  useHotkey("mod+z", draft.undo);
  useHotkey("mod+shift+z", draft.redo);

  const exportIt = async () => {
    setExporting(true);
    const f = fmt ?? formats[0]!;
    try {
      const name = await api.download("POST", `/artifacts/${kind}/export`, { body: draft.value, query: { fmt: f }, fallbackName: `${kind}.${f}` });
      toast.show(`Downloaded ${name}`);
    } catch (e) {
      toast.error(`Couldn't export: ${describeError(e)}`);
    } finally {
      setExporting(false);
    }
  };

  return (
    <>
      <div className={s.exportBar} style={{ position: "sticky", top: 8, zIndex: 5, background: "var(--canvas)", padding: "8px 0", borderBottom: "1px solid var(--rule)" }}>
        <Button size="sm" variant="ai" icon={<Icon.PenLine size={14} />} onClick={() => setTarget("*")}>Revise {meta.targets["*"]}</Button>
        <Button size="sm" variant="quiet" icon={<Icon.Undo2 size={15} />} disabled={!draft.canUndo} onClick={draft.undo} aria-label="Undo" />
        <Button size="sm" variant="quiet" icon={<Icon.Redo2 size={15} />} disabled={!draft.canRedo} onClick={draft.redo} aria-label="Redo" />
        <span style={{ flex: 1 }} />
        <Select aria-label="Format" value={fmt ?? formats[0]} onChange={(e) => setFmt(e.target.value)} style={{ width: "auto", minHeight: 30, padding: "3px 30px 3px 10px", fontSize: "var(--text-sm)" }}>
          {formats.map((f) => <option key={f} value={f}>.{f}</option>)}
        </Select>
        <Button size="sm" variant="primary" busy={exporting} icon={<Icon.Download size={15} />} onClick={() => void exportIt()}>Download</Button>
      </div>
      <div style={{ maxWidth: 780, marginTop: 16, display: "grid", gap: 10 }}>
        <Banner tone="info">Not saved to your library — this {meta.noun} is kept on this device until you download it.</Banner>
        {notes.length > 0 && <Banner tone="warning" title="Notes from the generator"><ul style={{ margin: "4px 0 0", paddingLeft: 18 }}>{notes.map((n, i) => <li key={i}>{n}</li>)}</ul></Banner>}
        <Editor value={draft.value} edit={draft.update} onRevise={setTarget} />
        {!!draft.value.grounding_sources?.length && <p style={{ fontSize: "var(--text-xs)", color: "var(--ink-3)", marginTop: 16 }}>Grounded in: {draft.value.grounding_sources.join(" · ")}</p>}
      </div>
      {target && (
        <ReviseDialog<Artifact> open onOpenChange={(o) => !o && setTarget(null)} label={target === "*" ? meta.targets["*"]! : `“${meta.targets[target] ?? target}”`}
          context={`the whole ${meta.noun}`} revision={draft.revision} sectionLabel={(k) => meta.targets[k] ?? k}
          propose={(instruction) => api.post<Proposal<Artifact>>(`/artifacts/${kind}/refine`, { artifact: draft.value, target, instruction })}
          onAccept={(c) => { draft.update(() => c); toast.show("Change applied", { action: { label: "Undo", run: draft.undo } }); }} />
      )}
    </>
  );
}
