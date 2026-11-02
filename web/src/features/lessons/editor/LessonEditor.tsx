import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Link, useNavigate } from "react-router-dom";
import { describeError, useApi, type RefineResult } from "@/lib/api";
import { relativeTime } from "@/lib/format";
import { modKeyLabel, safeStorage, useDraft, useHotkey, useUnsavedGuard } from "@/lib/hooks";
import { Badge, Banner, Button, ConfirmDialog, Icon, IconButton, Menu, SeamlessText, StatusDot, useToast } from "@/ui";
import { useExportLesson, useLiveValidation, type ExportKind } from "../api";
import { FRAMEWORKS, LANGUAGES, normalizeLesson, type Lesson } from "../model/ldd";
import { Inspector, type InspectorTab } from "./Inspector";
import { Outline } from "./Outline";
import { ReviseDialog } from "./ReviseDialog";
import { SECTION_TITLE, SECTIONS, sectionStatus, type SectionDef } from "./sections/registry";
import s from "./editor.module.css";

/**
 * Where a lesson lives. The editor never knows whether it is editing a saved
 * document, one day of a unit, or (in Edboox) something else entirely — it only
 * calls these. See `sources.ts` for the built-in adapters.
 */
export interface LessonSource {
  /** Stable key for local draft recovery, e.g. `doc:<id>`. */
  key: string;
  /** The server version the draft is based on; a stored draft only restores onto the same one. */
  baseVersion: string | null;
  /** Enables the History tab (server versions). */
  documentId: string | null;
  readOnly: boolean;
  crumbs: { label: string; to: string }[];
  /** Persist; may return a notice to show (e.g. unit coherence after a day edit). */
  save: (lesson: Lesson) => Promise<SaveOutcome>;
  /** Query string for "make a quiz/worksheet from this" (e.g. `lesson=<id>`); omitted = hidden. */
  studioQuery?: string;
  /** Shown above a read-only lesson (e.g. "Copy to my work"). */
  readOnlyNotice?: ReactNode;
  /** The server state changed underneath (history moved) — reload and remount. */
  reload?: () => void;
}

export interface SaveOutcome { versionId?: string | null; notice?: ReactNode }
interface StoredDraft { base: string | null; at: string; lesson: Lesson }
const draftKey = (k: string) => `lf.draft.${k}`;

export function LessonEditor({ initial, source }: { initial: Lesson; source: LessonSource }) {
  const api = useApi();
  const toast = useToast();
  const navigate = useNavigate();
  const draft = useDraft<Lesson>(initial);
  const lesson = draft.value;
  const readOnly = source.readOnly;

  // The server version local drafts are based on. Moves forward on each save; a
  // reload from the server remounts the editor (the page keys it), never resets
  // a draft underneath the teacher's cursor.
  const baseRef = useRef(source.baseVersion);

  const validation = useLiveValidation(lesson, draft.revision, source.key);
  const issues = validation.issues;
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState<ReactNode>(null);
  const [revise, setRevise] = useState<{ target: string } | null>(null);
  const [inspector, setInspector] = useState<{ open: boolean; tab: InspectorTab }>({ open: false, tab: "checks" });
  const exportLesson = useExportLesson();

  // ── local draft recovery ────────────────────────────────────────────────
  const [recovered, setRecovered] = useState<StoredDraft | null>(() => {
    if (readOnly) return null;
    const d = safeStorage.get<StoredDraft>(draftKey(source.key));
    return d && d.base === baseRef.current && JSON.stringify(d.lesson) !== JSON.stringify(initial) ? d : null;
  });
  useEffect(() => {
    if (readOnly || !draft.dirty) return;
    const t = setTimeout(() => safeStorage.set(draftKey(source.key), { base: baseRef.current, at: new Date().toISOString(), lesson } satisfies StoredDraft), 800);
    return () => clearTimeout(t);
  }, [lesson, draft.dirty, readOnly, source.key]);

  // ── save ───────────────────────────────────────────────────────────────
  const save = useCallback(async () => {
    if (readOnly || saving || !draft.dirty) return;
    const rev = draft.revision;
    setSaving(true);
    try {
      const out = await source.save(lesson);
      draft.markSaved(rev);
      if (out.versionId) baseRef.current = out.versionId;
      safeStorage.remove(draftKey(source.key));
      setNotice(out.notice ?? null);
      toast.show("Saved as a new version");
    } catch (e) {
      toast.error(`Couldn't save: ${describeError(e)}`);
    } finally {
      setSaving(false);
    }
  }, [readOnly, saving, draft, source, lesson, toast]);

  useHotkey("mod+s", () => void save(), { inFields: true, enabled: !readOnly });
  useHotkey("mod+z", draft.undo, { enabled: !readOnly });
  useHotkey("mod+shift+z", draft.redo, { enabled: !readOnly });
  const blocker = useUnsavedGuard(draft.dirty && !readOnly);

  // ── navigation within the document ─────────────────────────────────────
  const [active, setActive] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set());
  const jump = useCallback((key: string) => {
    setExpanded((x) => new Set(x).add(key));
    requestAnimationFrame(() => {
      const el = document.getElementById(`sec-${key}`);
      el?.scrollIntoView({ behavior: "smooth", block: "start" });
      el?.classList.add(s.flash!);
      setTimeout(() => el?.classList.remove(s.flash!), 1200);
    });
  }, []);
  useEffect(() => {
    const els = SECTIONS.map((d) => document.getElementById(`sec-${d.key}`)).filter(Boolean) as HTMLElement[];
    const io = new IntersectionObserver((entries) => {
      const top = entries.filter((e) => e.isIntersecting).sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top)[0];
      if (top) setActive(top.target.id.replace("sec-", ""));
    }, { rootMargin: "-80px 0px -60% 0px" });
    els.forEach((el) => io.observe(el));
    return () => io.disconnect();
  }, []);

  // ── export ─────────────────────────────────────────────────────────────
  const runExport = (kind: ExportKind) => {
    if (validation.valid === false) {
      toast.show(`Fix ${issues.length} guardrail issue${issues.length === 1 ? "" : "s"} first — exports compile from this lesson.`, { tone: "danger" });
      setInspector({ open: true, tab: "checks" });
      return;
    }
    exportLesson.mutate({ ldd: lesson, kind }, {
      onSuccess: (name) => toast.show(`Downloaded ${name}`),
      onError: (e) => toast.error(describeError(e)),
    });
  };

  const proposeFor = (target: string) => (instruction: string) =>
    api.post<RefineResult>("/lessons/refine", { ldd: lesson, target, instruction }).then((r) => ({
      ...r, candidate: r.candidate ? normalizeLesson(r.candidate) : null,
    }));

  const errorCount = issues.length;
  const saveState = saving ? "Saving…" : draft.dirty ? "Unsaved changes" : "All changes saved";

  return (
    <>
      <div className={s.toolbar} style={{ ["--toolbar-h" as string]: "60px" }}>
        <div className={s.toolbarInner}>
          <div className={s.crumbs}>
            {source.crumbs.map((c) => <Link key={c.to} to={c.to}><Icon.ArrowLeft size={14} />{c.label}</Link>)}
          </div>
          <span className={s.tbSpacer} />
          {!readOnly && <span className={`${s.saveState} ${s.hideSm}`} data-state={draft.dirty ? "dirty" : "clean"} aria-live="polite">{saveState}</span>}
          {!readOnly && (
            <div className={`${s.tbGroup} ${s.hideSm}`}>
              <IconButton label={`Undo (${modKeyLabel}Z)`} disabled={!draft.canUndo} onClick={draft.undo}><Icon.Undo2 size={17} /></IconButton>
              <IconButton label={`Redo (${modKeyLabel}⇧Z)`} disabled={!draft.canRedo} onClick={draft.redo}><Icon.Redo2 size={17} /></IconButton>
            </div>
          )}
          <Button size="sm" variant="quiet" onClick={() => setInspector({ open: true, tab: "checks" })}
            icon={validation.valid === null || validation.checking ? <Icon.Loader2 size={15} className="lf-spin" /> : <StatusDot status={errorCount ? "error" : "ok"} />}>
            <span className={s.hideSm}>{errorCount ? `${errorCount} to fix` : "Checks"}</span>
          </Button>
          <Menu trigger={<Button size="sm" icon={<Icon.Download size={15} />} busy={exportLesson.isPending}><span className={s.hideSm}>Export</span></Button>} items={[
            { heading: "Download" },
            { label: "Everything (.zip)", icon: <Icon.Layers size={16} />, onSelect: () => runExport("bundle") },
            { label: "Lesson plan", icon: <Icon.FileText size={16} />, onSelect: () => runExport("lesson_plan") },
            { label: "Slides", icon: <Icon.Presentation size={16} />, onSelect: () => runExport("slides") },
            { label: "Worksheet", icon: <Icon.ClipboardList size={16} />, onSelect: () => runExport("worksheet") },
            { label: "Quiz", icon: <Icon.ListChecks size={16} />, onSelect: () => runExport("quiz") },
            ...(source.studioQuery ? ["separator" as const, { heading: "Build from this lesson" },
              { label: "A quiz…", icon: <Icon.ListChecks size={16} />, onSelect: () => navigate(`/studio/quiz?${source.studioQuery}`) },
              { label: "A worksheet…", icon: <Icon.ClipboardList size={16} />, onSelect: () => navigate(`/studio/worksheet?${source.studioQuery}`) }] : []),
            "separator",
            { label: "Details & history", icon: <Icon.PanelRight size={16} />, onSelect: () => setInspector({ open: true, tab: "quality" }) },
          ]} />
          {!readOnly && (
            <Button size="sm" variant="primary" icon={<Icon.Save size={15} />} busy={saving} disabled={!draft.dirty} onClick={() => void save()}
              title={`Save (${modKeyLabel}S)`}>Save</Button>
          )}
        </div>
      </div>

      <div className={s.layout}>
        <aside className={s.outlineCol}><Outline lesson={lesson} issues={issues} active={active} onJump={jump} /></aside>

        <article className={s.doc}>
          <header className={s.docHead}>
            <div className={s.kicker}>
              <Badge tone="pine">Grade {lesson.curriculum_ref.grade} · {lesson.curriculum_ref.subject}</Badge>
              <Badge>{lesson.duration_min} min</Badge>
              <Badge>{FRAMEWORKS.find((f) => f.value === lesson.framework)?.label}</Badge>
              <Badge>{LANGUAGES.find((l) => l.value === lesson.language)?.label}</Badge>
              {readOnly && <Badge tone="marigold"><Icon.Lock size={11} /> Read only</Badge>}
            </div>
            <SeamlessText className={s.topic} value={lesson.topic} readOnly={readOnly} aria-label="Topic" placeholder="What is this lesson about?"
              onValue={(v) => draft.update((d) => { d.topic = v; }, "topic")} />
            {!readOnly && (
              <div style={{ marginTop: 12 }}>
                <Button size="sm" variant="ai" icon={<Icon.PenLine size={15} />} onClick={() => setRevise({ target: "*" })}>Revise the whole lesson with AI</Button>
              </div>
            )}
          </header>

          <div style={{ display: "grid", gap: 10, marginBottom: 16 }}>
            {readOnly && source.readOnlyNotice}
            {recovered && (
              <Banner tone="warning" title="You have unsaved edits from earlier"
                actions={<>
                  <Button size="sm" variant="primary" onClick={() => { draft.update(() => recovered.lesson); setRecovered(null); }}>Restore</Button>
                  <Button size="sm" variant="quiet" onClick={() => { safeStorage.remove(draftKey(source.key)); setRecovered(null); }}>Discard</Button>
                </>}>
                Kept on this device {relativeTime(recovered.at)}. Restore them on top of the saved lesson?
              </Banner>
            )}
            {notice}
          </div>

          {SECTIONS.map((def) => (
            <SectionBlock key={def.key} def={def} lesson={lesson} issues={issues} readOnly={readOnly}
              expanded={!def.secondary || expanded.has(def.key) || issues.some((i) => i.section === def.key)}
              onToggle={() => setExpanded((x) => { const n = new Set(x); if (n.has(def.key)) n.delete(def.key); else n.add(def.key); return n; })}
              edit={draft.update} onRevise={() => setRevise({ target: def.refine! })} />
          ))}
        </article>
      </div>

      <Inspector open={inspector.open} onOpenChange={(o) => setInspector((x) => ({ ...x, open: o }))} tab={inspector.tab}
        onTab={(t) => setInspector({ open: true, tab: t })} lesson={lesson} validation={validation} documentId={source.documentId}
        dirty={draft.dirty} onJump={jump} onHistoryMoved={() => source.reload?.()} />

      {revise && (
        <ReviseDialog<Lesson> open onOpenChange={(o) => !o && setRevise(null)}
          label={revise.target === "*" ? "the whole lesson" : `“${SECTION_TITLE[revise.target] ?? revise.target}”`}
          context="the whole lesson" revision={draft.revision} propose={proposeFor(revise.target)}
          sectionLabel={(k) => SECTION_TITLE[k] ?? k}
          onAccept={(candidate) => { draft.update(() => candidate); toast.show("Change applied — save to keep it", { action: { label: "Undo", run: draft.undo } }); }} />
      )}

      <ConfirmDialog open={blocker.state === "blocked"} onOpenChange={(o) => { if (!o) blocker.reset?.(); }}
        title="Leave without saving?" body="Your edits stay on this device and you'll be offered them next time — but they aren't saved to your library yet."
        confirmLabel="Leave" danger onConfirm={() => blocker.proceed?.()} />
    </>
  );
}

function SectionBlock({ def, lesson, issues, readOnly, expanded, onToggle, edit, onRevise }: {
  def: SectionDef; lesson: Lesson; issues: ReturnType<typeof useLiveValidation>["issues"]; readOnly: boolean;
  expanded: boolean; onToggle: () => void; edit: ReturnType<typeof useDraft<Lesson>>["update"]; onRevise: () => void;
}) {
  const own = useMemo(() => issues.filter((i) => i.section === def.key), [issues, def.key]);
  const Body = def.Component;
  return (
    <section id={`sec-${def.key}`} className={`${s.block} ${expanded ? "" : s.collapsed}`} aria-labelledby={`sec-${def.key}-h`}>
      <div className={s.blockHead}>
        {def.secondary && (
          <button type="button" className={s.chev} aria-expanded={expanded} aria-controls={`sec-${def.key}-body`} onClick={onToggle}
            aria-label={`${expanded ? "Collapse" : "Expand"} ${def.title}`}>
            <Icon.ChevronDown size={18} />
          </button>
        )}
        <h2 id={`sec-${def.key}-h`} className={s.blockTitle}>
          {def.title} <StatusDot status={sectionStatus(def, lesson, issues)} />
        </h2>
        {!expanded && <span className={s.summary}>{def.summary(lesson)}</span>}
        <div className={s.blockActions}>
          {def.refine && !readOnly && expanded && (
            <Button size="sm" variant="ai" icon={<Icon.PenLine size={14} />} onClick={onRevise} aria-label={`Revise ${def.title} with AI`}>
              <span className={s.hideSm}>Revise</span>
            </Button>
          )}
        </div>
      </div>
      {expanded && (
        <div id={`sec-${def.key}-body`}>
          {def.hint && <p className={s.blockHint}>{def.hint}</p>}
          <Body lesson={lesson} edit={edit} readOnly={readOnly} issues={own} />
        </div>
      )}
    </section>
  );
}
