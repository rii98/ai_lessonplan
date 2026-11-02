import { useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { SourcesTree } from "@/features/grounding";
import { FRAMEWORKS, LANGUAGES, useCreateLesson } from "@/features/lessons";
import { useProfile } from "@/features/profile";
import { useMe } from "@/features/session";
import type { StudioHandoff } from "@/features/studio";
import type { UnitPlannerState } from "@/features/units";
import { describeError, useApi, type Page as PageOf, type StoredDocument } from "@/lib/api";
import { relativeTime } from "@/lib/format";
import { useElapsed, useStoredState } from "@/lib/hooks";
import { Banner, Button, EmptyState, Icon, Page, ProgressPanel, Section, Sheet, Skeleton, TextArea } from "@/ui";
import { useQuery } from "@tanstack/react-query";
import { KIND_ORDER, KINDS, type CreateKind } from "./kinds";
import s from "./create.module.css";

interface Brief {
  topic: string;
  grade: string;
  subject: string;
  duration: string;
  language: string;
  framework: string;
  days: string;
  existingPlan: string;
}
const EMPTY: Brief = { topic: "", grade: "", subject: "", duration: "", language: "", framework: "", days: "5", existingPlan: "" };

const greeting = () => { const h = new Date().getHours(); return h < 12 ? "Good morning" : h < 17 ? "Good afternoon" : "Good evening"; };
const orNull = (v: string) => (v.trim() ? v.trim() : null);
const numOrNull = (v: string) => (v.trim() ? Number(v) : null);

/** `/` — "what are you teaching?" One composer for everything a teacher can make. */
export function HomePage() {
  const navigate = useNavigate();
  const me = useMe().data;
  const profile = useProfile().data;
  const [kind, setKind] = useStoredState<CreateKind>("lf.home.kind", "lesson");
  const [brief, setBriefRaw] = useStoredState<Brief>("lf.home.brief", EMPTY);
  const [showPlan, setShowPlan] = useState(() => !!brief.existingPlan);
  const [sources, setSources] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const create = useCreateLesson();
  const elapsed = useElapsed(create.isPending);
  const topicRef = useRef<HTMLTextAreaElement>(null);
  const k = KINDS[kind];

  const set = (key: keyof Brief, v: string) => setBriefRaw({ ...brief, [key]: v });
  useEffect(() => {
    const el = topicRef.current;
    if (el) { el.style.height = "auto"; el.style.height = `${el.scrollHeight}px`; }
  }, [brief.topic]);

  // explicit values win; blanks fall back to the profile on the server
  const grade = brief.grade || (profile?.default_grade ? String(profile.default_grade) : "");
  const subject = brief.subject || profile?.default_subject || "";

  const submit = () => {
    setError(null);
    const topic = brief.topic.trim();
    if (!topic && !(kind === "lesson" && brief.existingPlan.trim())) { setError("Tell us what you're teaching first."); topicRef.current?.focus(); return; }
    const common = {
      topic: topic || null, grade: numOrNull(brief.grade), subject: orNull(brief.subject),
      duration_min: numOrNull(brief.duration) as 30 | 45 | 60 | null, language: orNull(brief.language) as "en" | "ne" | "en-ne" | null,
      framework: orNull(brief.framework) as "5E" | "gradual_release" | "inquiry" | null,
    };
    if (kind === "lesson") {
      create.mutate({ ...common, existing_plan: showPlan ? orNull(brief.existingPlan) : null }, {
        onSuccess: (saved) => { setBriefRaw(EMPTY); navigate(`/lessons/${saved.document.id}`); },
        onError: (e) => setError(describeError(e)),
      });
    } else if (kind === "unit") {
      if (!grade || !subject) { setError("A unit needs a grade and a subject."); return; }
      const state: UnitPlannerState = {
        autoplan: true,
        brief: { topic, grade: Number(grade), subject, num_days: Number(brief.days) || 5, duration_min: common.duration_min ?? profile?.default_duration_min ?? 45,
          language: common.language ?? (profile?.default_language as "en" | "ne" | "en-ne" | undefined) ?? "en-ne",
          framework: common.framework ?? (profile?.default_framework as "5E" | "gradual_release" | "inquiry" | undefined) ?? "5E" },
      };
      navigate("/units/new", { state });
    } else {
      const state: StudioHandoff = { brief: common };
      navigate(`/studio/${kind}`, { state });
    }
  };

  const name = (profile?.teacher_name || me?.name || "").split(" ")[0];

  return (
    <Page width="narrow">
      <div className={s.hero}>
        <h1 className={s.greet}>{greeting()}{name ? `, ${name}` : ""}.</h1>
        <p className={s.lede}>What are you teaching next?</p>
      </div>

      <form className={s.composer} onSubmit={(e) => { e.preventDefault(); submit(); }} aria-busy={create.isPending}>
        <div className={s.kinds} role="radiogroup" aria-label="What to make">
          {KIND_ORDER.map((id) => (
            <button key={id} type="button" role="radio" aria-checked={kind === id} className={s.kind} disabled={create.isPending} onClick={() => setKind(id)}>
              {KINDS[id].icon}{KINDS[id].label}
            </button>
          ))}
        </div>
        <textarea ref={topicRef} className={s.topic} rows={2} value={brief.topic} placeholder={k.placeholder} aria-label="What are you teaching?"
          disabled={create.isPending} onChange={(e) => set("topic", e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) submit(); }} />
        <p className={s.kindHint}>{k.hint}</p>

        <div className={s.params}>
          <label className={s.param}>Grade<input className={s.short} inputMode="numeric" value={brief.grade} placeholder={profile?.default_grade ? String(profile.default_grade) : "—"}
            onChange={(e) => set("grade", e.target.value.replace(/\D/g, "").slice(0, 2))} /></label>
          <label className={s.param}>Subject<input value={brief.subject} placeholder={profile?.default_subject || "—"} onChange={(e) => set("subject", e.target.value)} /></label>
          {kind === "unit" && <label className={s.param}>Days<input className={s.short} inputMode="numeric" value={brief.days} onChange={(e) => set("days", e.target.value.replace(/\D/g, "").slice(0, 2))} /></label>}
          {(kind === "lesson" || kind === "unit") && (
            <label className={s.param}>Period
              <select value={brief.duration} onChange={(e) => set("duration", e.target.value)}>
                <option value="">{profile?.default_duration_min ? `${profile.default_duration_min} min` : "default"}</option>
                <option value="30">30 min</option><option value="45">45 min</option><option value="60">60 min</option>
              </select>
            </label>
          )}
          <label className={s.param}>Language
            <select value={brief.language} onChange={(e) => set("language", e.target.value)}>
              <option value="">{LANGUAGES.find((l) => l.value === profile?.default_language)?.label ?? "default"}</option>
              {LANGUAGES.map((l) => <option key={l.value} value={l.value}>{l.label}</option>)}
            </select>
          </label>
          {(kind === "lesson" || kind === "unit") && (
            <label className={s.param}>Framework
              <select value={brief.framework} onChange={(e) => set("framework", e.target.value)}>
                <option value="">{FRAMEWORKS.find((f) => f.value === profile?.default_framework)?.label ?? "default"}</option>
                {FRAMEWORKS.map((f) => <option key={f.value} value={f.value}>{f.label}</option>)}
              </select>
            </label>
          )}
        </div>

        {kind === "lesson" && (
          <div className={s.extra}>
            {showPlan ? (
              <TextArea autoGrow rows={4} value={brief.existingPlan} disabled={create.isPending} aria-label="Your draft plan"
                placeholder="Paste your rough plan. We keep your intent and add a hook, misconceptions, active learning and aligned checks."
                onChange={(e) => set("existingPlan", e.target.value)} />
            ) : (
              <button type="button" className={s.linkish} onClick={() => setShowPlan(true)}><Icon.Plus size={14} /> Start from my own draft plan</button>
            )}
          </div>
        )}

        {create.isPending ? (
          <div style={{ padding: "0 20px 20px" }}>
            <ProgressPanel title="Writing your lesson" elapsed={elapsed} stages={[
              { label: "Reading your brief", detail: "topic, grade and your profile defaults" },
              { label: "Grounding", detail: "looking up the curriculum, misconceptions and local context" },
              { label: "Drafting & reviewing", detail: "writing the plan, then critiquing and revising its weak parts" },
            ]} note="Usually 30–90 seconds. It's saved to your library automatically." />
          </div>
        ) : (
          <div className={s.foot}>
            <Button type="submit" variant="primary" icon={kind === "lesson" ? <Icon.PenLine size={16} /> : <Icon.ArrowRight size={16} />}>{k.cta}</Button>
            <button type="button" className={s.linkish} disabled={!brief.topic.trim()} onClick={() => setSources(true)}><Icon.BookOpen size={14} /> What will it draw on?</button>
            <span className={s.footNote}>Blank fields use your <Link to="/settings">profile</Link>.</span>
          </div>
        )}
        {error && <div style={{ padding: "0 20px 16px" }}><Banner tone="danger">{error}</Banner></div>}
      </form>

      <Section title="Recent work" actions={<Link to="/library" style={{ fontSize: "var(--text-sm)" }}>Open library →</Link>} className="">
        <RecentWork />
      </Section>

      <Sheet open={sources} onOpenChange={setSources} title="What it will draw on" description="The reference material retrieved for this topic — the same lookup generation uses.">
        <SourcesTree query={sources ? { topic: brief.topic, grade: numOrNull(grade), subject: orNull(subject), framework: orNull(brief.framework) } : null} />
      </Sheet>
    </Page>
  );
}

function RecentWork() {
  const api = useApi();
  const q = useQuery({
    queryKey: ["documents", "recent"],
    queryFn: ({ signal }) => api.get<PageOf<StoredDocument>>("/documents", { scope: "mine", limit: 6 }, signal),
  });
  if (q.isPending) return <div style={{ display: "grid", gap: 12 }}>{[0, 1, 2].map((i) => <Skeleton key={i} h={36} />)}</div>;
  if (q.error) return <Banner tone="danger">{describeError(q.error)}</Banner>;
  if (!q.data.items.length) return <EmptyState title="Nothing yet">Your lessons and units appear here as soon as you make them.</EmptyState>;
  return (
    <div className={s.recent}>
      {q.data.items.map((d) => (
        <Link key={d.id} to={d.kind === "unit" ? `/units/${d.id}` : `/lessons/${d.id}`} className={s.recentRow}>
          <span className={s.recentIcon}>{d.kind === "unit" ? <Icon.CalendarRange size={16} /> : <Icon.FileText size={16} />}</span>
          <span style={{ minWidth: 0 }}>
            <div className={s.recentTitle}>{d.title || "Untitled"}</div>
            <div className={s.recentMeta}>{d.kind === "unit" ? "Unit" : "Lesson"}{d.grade ? ` · Grade ${d.grade}` : ""}{d.subject ? ` · ${d.subject}` : ""}</div>
          </span>
          <span className={s.when}>{relativeTime(d.updated_at)}</span>
        </Link>
      ))}
    </div>
  );
}
