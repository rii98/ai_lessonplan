import { forwardRef, Fragment, Suspense, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { useMe } from "@/features/session";
import { describeError, type Conversation } from "@/lib/api";
import { relativeTime, truncate } from "@/lib/format";
import {
  Banner, Button, ConfirmDialog, Dialog, Field, Icon, IconButton, Menu, NumberInput, Popover, Segmented, Sheet, SkeletonLines, Spinner, TextInput, useToast,
} from "@/ui";
import { chatKeys, useChatConfig, useConversation, useConversationMutations, useConversations, useEndLesson, useLearningSession } from "./api";
import { ARTIFACT_RENDERERS } from "./artifacts/registry";
import { LearnWidget } from "./learn/LearnWidget";
import { MemoryDialog } from "./learn/MemoryDialog";
import { ProgressPath } from "./learn/ProgressPath";
import { QuickChips } from "./learn/QuickChips";
import learnCss from "./learn/learn.module.css";
import { AssistantMessage, UserMessage, userKind } from "./Message";
import type { ArtifactSummary, ChatConfig, Citation, ConversationDetail, LearnAction, LearnFeedback, LearnWidget as Widget, Scope } from "./types";
import { useChatTurn, type TurnInput } from "./useChatTurn";
import s from "./chat.module.css";

const SUGGESTIONS = [
  "What misconceptions do Grade 6 students have about photosynthesis?",
  "Summarise the learning outcomes for Grade 8 Science.",
  "Give me a local-context hook for teaching fractions.",
  "Quiz me on the water cycle.",
];

/** How a widget answer reads in the transcript (matches the server's wording). */
export function answerLabel(w: Widget, response: unknown): string {
  if (typeof response === "number" && w.choices?.[response] != null) return `Answered: ${w.choices[response]}`;
  if (Array.isArray(response)) return "Answered: (arranged the items)";
  return `Answered: ${String(response ?? "").slice(0, 200)}`;
}

function scopeFrom(cfg: ChatConfig | undefined, defaults?: Partial<Scope> | null): Scope {
  const d = { ...cfg?.scope_defaults, ...defaults };
  return { mode: d.mode ?? cfg?.default_mode ?? "scoped", grade: d.grade ?? null, subject: d.subject ?? null, class: d.class ?? null, collections: d.collections ?? null };
}

/** `/chat/:conversationId?` — grounded answers with sources, and interactive quizzes. */
export function ChatPage() {
  const { conversationId } = useParams();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const toast = useToast();
  const me = useMe().data;
  const solo = (me?.nav.length ?? 0) <= 1;
  const cfg = useChatConfig().data;
  const list = useConversations();
  const detail = useConversation(conversationId);
  const muts = useConversationMutations();
  const chat = useChatTurn();
  const [scope, setScope] = useState<Scope>(() => scopeFrom(cfg));
  const [text, setText] = useState("");
  const [citation, setCitation] = useState<Citation | null>(null);
  const [canvas, setCanvas] = useState<string | null>(null);
  const [convosOpen, setConvosOpen] = useState(false);
  const [learnMode, setLearnMode] = useState(false);
  const [memoryOpen, setMemoryOpen] = useState(false);
  const learning = useLearningSession(conversationId);
  const endLesson = useEndLesson(conversationId);
  const lesson = learning.data?.session ?? null;
  const lessonActive = lesson?.status === "active" || lesson?.status === "planning";
  const scrollRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  // scope follows the open conversation's saved defaults
  useEffect(() => { setScope(scopeFrom(cfg, detail.data?.conversation.defaults as Partial<Scope> | undefined)); }, [cfg, detail.data?.conversation.id]); // eslint-disable-line react-hooks/exhaustive-deps
  // switching chats drops the previous turn — but not the one that just created
  // this conversation (its first answer is still streaming in)
  useEffect(() => {
    setCanvas(null);
    if (chat.turn && chat.turn.conversationId !== conversationId) chat.clear();
  }, [conversationId]); // eslint-disable-line react-hooks/exhaustive-deps

  const messages = useMemo(() => detail.data?.messages ?? [], [detail.data?.messages]);
  const artifacts = useMemo(() => detail.data?.artifacts ?? [], [detail.data?.artifacts]);
  const live = chat.turn && chat.turn.conversationId === conversationId && !chat.turn.done ? chat.turn : null;
  const failed = chat.turn && chat.turn.conversationId === conversationId && chat.turn.done ? chat.turn.error : null;

  // keep the newest content in view while streaming (unless the reader scrolled up)
  const pinned = useRef(true);
  useLayoutEffect(() => {
    const el = scrollRef.current;
    if (el && pinned.current) el.scrollTop = el.scrollHeight;
  }, [messages.length, live?.content, live?.status]);

  /** Run one turn: a typed message, a learn-mode input, or the start of a lesson. */
  const go = async (input: TurnInput) => {
    if (chat.streaming) return;
    let id = conversationId;
    if (!id) {
      try {
        const conv = await muts.create.mutateAsync(scope);
        qc.setQueryData<ConversationDetail>(chatKeys.detail(conv.id), { conversation: conv, messages: [], artifacts: [] });
        id = conv.id;
        navigate(`/chat/${conv.id}`, { replace: true });
      } catch (e) { toast.error(describeError(e)); return; }
    }
    if (input.kind !== "learn") setText("");
    pinned.current = true;
    const t = await chat.run(id, input, scope);
    const art = t.artifacts[t.artifacts.length - 1];
    if (art) setCanvas(art.id);
    if (t.learn) setLearnMode(false);   // the lesson now owns the chat; the toggle has done its job
    inputRef.current?.focus();
  };

  const send = (raw?: string) => {
    const msg = (raw ?? text).trim();
    if (!msg) return;
    // the Learn toggle turns the next message into a lesson topic (unless one is running)
    return go(learnMode && !lessonActive ? { kind: "start", topic: msg } : { kind: "message", text: msg });
  };
  const act = (action: LearnAction, label: string, value?: string) => void go({ kind: "learn", label, action, value });
  const answer = (w: Widget, response: unknown, confidence?: number) =>
    void go({ kind: "learn", label: answerLabel(w, response), widget: { widget_id: w.id, response, confidence } });

  const upsertArtifact = (a: ArtifactSummary) => qc.setQueryData<ConversationDetail>(chatKeys.detail(conversationId ?? "-"), (prev) => prev && ({
    ...prev, artifacts: prev.artifacts.some((x) => x.id === a.id) ? prev.artifacts.map((x) => (x.id === a.id ? a : x)) : [...prev.artifacts, a],
  }));

  const artifactById = useMemo(() => new Map(artifacts.map((a) => [a.id, a])), [artifacts]);
  // learn mode: grading arrives with the NEXT turn, keyed by the widget it grades
  const feedbackFor = useMemo(() => {
    const m = new Map<string, LearnFeedback>();
    for (const x of messages) { const f = x.meta?.feedback as LearnFeedback | undefined; if (f) m.set(f.widget_id, f); }
    if (chat.turn?.feedback) m.set(chat.turn.feedback.widget_id, chat.turn.feedback);
    return m;
  }, [messages, chat.turn?.feedback]);
  const pendingWidget = lessonActive ? learning.data?.widget ?? null : null;
  const lastAssistant = messages.reduce((n, m, i) => (m.role === "assistant" ? i : n), -1);
  // name the speaker only where the mode changes (Q&A → lesson → Q&A), not on every turn
  const skillOf = (m: { meta?: { intent?: { skill?: string } | null } }) => (m.meta?.intent?.skill && m.meta.intent.skill !== "qa" ? m.meta.intent.skill : "qa");
  const speakerShown = useMemo(() => {
    let prev: string | null = null;
    return messages.map((m) => { if (m.role !== "assistant") return false; const k = skillOf(m); const show = k !== prev; prev = k; return show; });
  }, [messages]);
  const lastSkill = lastAssistant >= 0 ? skillOf(messages[lastAssistant]!) : null;
  const widgetOf = (meta: Record<string, unknown> | undefined): Widget | null => {
    const w = meta?.widget as Widget | undefined;
    if (w) return w;
    const wid = (meta?.learn as { widget_id?: string } | undefined)?.widget_id;
    return wid && pendingWidget?.id === wid ? pendingWidget : null;
  };
  const learnExtras = (meta: Record<string, unknown> | undefined, isLast: boolean, pending: boolean) => {
    const w = widgetOf(meta);
    const offer = meta?.offer as string | undefined;
    const fb = w ? feedbackFor.get(w.id) ?? null : null;
    const canAnswer = !!w && !fb && !pending && isLast && pendingWidget?.id === w.id;
    return (<>
      {w && <LearnWidget widget={w} feedback={fb} busy={chat.streaming} onAnswer={canAnswer ? (r, c) => answer(w, r, c) : undefined} />}
      {offer && isLast && !pending && !lessonActive && (
        <div className={learnCss.offer}>
          <Icon.GraduationCap size={16} /><span style={{ flex: 1 }}>Learn <b>{offer}</b> step by step?</span>
          <Button size="sm" variant="primary" onClick={() => void go({ kind: "start", topic: offer })}>Start lesson</Button>
        </div>
      )}
      {isLast && !pending && lessonActive && lesson && <QuickChips state={lesson} onAction={act} />}
    </>);
  };
  const openArtifact = artifacts.find((a) => a.id === canvas) ?? live?.artifacts.find((a) => a.id === canvas) ?? null;
  const Renderer = openArtifact ? ARTIFACT_RENDERERS[openArtifact.kind] : null;
  const title = detail.data?.conversation.title || "New chat";
  const locked = cfg?.scope_mode === "locked";

  const sidebar = <ConversationList items={list.data} loading={list.isPending} activeId={conversationId} onPick={() => setConvosOpen(false)} muts={muts}
    onDeleted={(id) => { if (id === conversationId) navigate("/chat", { replace: true }); }} />;

  const canvasBody = openArtifact && Renderer && (
    <Suspense fallback={<SkeletonLines lines={4} />}>
      <Renderer id={openArtifact.id} onSummary={upsertArtifact} onNew={(a) => { upsertArtifact(a); setCanvas(a.id); }}
        onDeleted={(id) => { qc.setQueryData<ConversationDetail>(chatKeys.detail(conversationId ?? "-"), (p) => p && ({ ...p, artifacts: p.artifacts.filter((x) => x.id !== id) })); setCanvas(null); }} />
    </Suspense>
  );

  return (
    <div className={`${s.app} ${solo ? s.solo : ""} ${openArtifact ? s.canvasOpen : ""}`}>
      <aside className={s.side} aria-label="Conversations">
        <div className={s.sideHead}><Link to="/chat"><Button block icon={<Icon.Plus size={16} />}>New chat</Button></Link></div>
        {sidebar}
      </aside>

      <section className={s.main} aria-label="Chat">
        <div className={s.head}>
          <IconButton className={s.mobileOnly} label="Conversations" onClick={() => setConvosOpen(true)}><Icon.Menu size={18} /></IconButton>
          <span className={s.headTitle}>{conversationId ? title : "New chat"}</span>
          <ScopeControl scope={scope} onChange={setScope} cfg={cfg} locked={locked}
            onSaveDefault={conversationId ? () => muts.saveScope.mutate({ id: conversationId, scope }, { onSuccess: () => toast.show("Saved as this chat's default") }) : undefined} />
          <IconButton label="What LessonForge remembers" onClick={() => setMemoryOpen(true)}><Icon.Brain size={18} /></IconButton>
          {conversationId && <Link to="/chat" className={s.mobileOnly}><IconButton label="New chat"><Icon.Plus size={18} /></IconButton></Link>}
        </div>

        {lesson && (
          <ProgressPath state={lesson} busy={chat.streaming || endLesson.isPending}
            onPause={() => act("pause", "Take a break")} onResume={() => act("resume", "Continue the lesson")}
            onEnd={() => endLesson.mutate(lesson.id, { onSuccess: () => toast.show("Lesson ended — your progress is saved") })} />
        )}
        <div className={s.scroll} ref={scrollRef} onScroll={(e) => { const el = e.currentTarget; pinned.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80; }}>
          <div className={s.thread}>
            {conversationId && detail.isPending && <SkeletonLines lines={6} />}
            {detail.error && <Banner tone="danger" title="Couldn't open this chat">{describeError(detail.error)}</Banner>}
            {!conversationId && !live && (
              <div className={s.welcome}>
                <h2>Ask your curriculum</h2>
                <p style={{ color: "var(--ink-2)", maxWidth: "48ch" }}>Answers come from your textbooks and teaching notes, with the sources shown. Ask for a quiz and you can play it right here.</p>
                <div className={s.suggest}>{SUGGESTIONS.map((q) => <button key={q} type="button" onClick={() => void send(q)}>{q}</button>)}</div>
              </div>
            )}
            {messages.map((m, i) => m.role === "user"
              ? <UserMessage key={m.id ?? i} text={m.content} kind={userKind(m.meta)} />
              : <AssistantMessage key={m.id ?? i} content={m.content} citations={m.citations} skill={m.meta?.intent?.skill} showName={speakerShown[i]}
                  artifacts={(m.meta?.artifact_ids ?? []).map((id) => artifactById.get(id)).filter(Boolean) as ArtifactSummary[]}
                  onCite={setCitation} onOpenArtifact={setCanvas}
                  onQuickAction={i === messages.length - 1 && !lessonActive ? (q) => void send(q) : undefined}
                  onTeach={i === messages.length - 1 && !lessonActive && messages[i - 1]?.role === "user"
                    ? () => void go({ kind: "start", topic: messages[i - 1]!.content.slice(0, 200) }) : undefined}>
                  {learnExtras(m.meta, i === lastAssistant && !live, false)}
                </AssistantMessage>)}
            {live && (<>
              <UserMessage text={live.userText} kind={userKind(live.userMeta)} />
              <AssistantMessage pending content={live.content} citations={live.citations} artifacts={live.artifacts} status={live.status ?? (live.skill ? "Working on it…" : null)}
                skill={live.skill} showName={(live.skill ?? (live.userMeta?.mode === "learn" ? "learn" : lastSkill ?? "qa")) !== lastSkill} onCite={setCitation} onOpenArtifact={setCanvas}>
                {live.widget && <LearnWidget widget={live.widget} feedback={null} busy />}
              </AssistantMessage>
            </>)}
            {failed && <Banner tone="danger" title="That reply didn't finish">{failed}</Banner>}
          </div>
        </div>

        <form className={s.composer} onSubmit={(e) => { e.preventDefault(); void send(); }}>
          <div className={s.box}>
            <AutoTextarea ref={inputRef} value={text} onChange={setText} disabled={muts.create.isPending}
              placeholder={lessonActive ? "Type your answer, ask a question, or say “I don't know”…"
                : learnMode ? "What do you want to learn? e.g. photosynthesis"
                : chat.turn?.awaiting && !live ? `Answer: ${chat.turn.awaiting}…` : "Ask about a topic, or say “quiz me on…”"}
              onEnter={() => void send()} />
            {chat.streaming
              ? <Button className={s.send} variant="default" aria-label="Stop" title="Stop" onClick={chat.stop}><Icon.Square size={14} /></Button>
              : <Button className={s.send} type="submit" variant="primary" aria-label="Send" disabled={!text.trim()} busy={muts.create.isPending}><Icon.Send size={16} /></Button>}
          </div>
          <div className={s.hint}>
            {lessonActive
              ? <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}><Icon.GraduationCap size={12} /> In a lesson — your typed replies go to the tutor</span>
              : <button type="button" className={learnCss.learnToggle} aria-pressed={learnMode} onClick={() => setLearnMode(!learnMode)}
                  title="Learn step by step: guiding questions, hints and quick checks instead of direct answers">
                  <Icon.GraduationCap size={13} /> Learn mode{learnMode ? " on" : ""}
                </button>}
            <span>{chat.streaming ? "Answering…" : "Enter to send · Shift+Enter for a new line"}</span>
          </div>
        </form>
      </section>

      <aside className={s.canvas} aria-label={openArtifact?.title ?? "Artifact"}>
        <div className={s.canvasHead}>
          <span className={s.headTitle}>{openArtifact?.title}</span>
          {artifacts.length > 1 && (
            <Menu trigger={<Button size="sm" variant="quiet" icon={<Icon.ChevronDown size={14} />}>Switch</Button>}
              items={artifacts.map((a, i) => ({ label: `${i + 1}. ${truncate(a.title, 40)}`, onSelect: () => setCanvas(a.id) }))} />
          )}
          <IconButton label="Close" onClick={() => setCanvas(null)}><Icon.X size={18} /></IconButton>
        </div>
        <div className={s.canvasBody}>{canvasBody}</div>
      </aside>

      {/* phones: the canvas and conversation list are sheets */}
      <MemoryDialog open={memoryOpen} onOpenChange={setMemoryOpen} />
      <MobileSheet open={!!openArtifact} title={openArtifact?.title ?? ""} onClose={() => setCanvas(null)}>{canvasBody}</MobileSheet>
      <Sheet open={convosOpen} onOpenChange={setConvosOpen} title="Conversations">
        <div style={{ marginBottom: 12 }}><Link to="/chat" onClick={() => setConvosOpen(false)}><Button block icon={<Icon.Plus size={16} />}>New chat</Button></Link></div>
        {sidebar}
      </Sheet>
      <Sheet open={!!citation} onOpenChange={(o) => !o && setCitation(null)} title={citation ? `Source ${citation.n}` : ""}
        description={citation ? [citation.collection?.replace("_", " "), citation.source].filter(Boolean).join(" · ") : undefined}>
        {citation && (<>
          <p style={{ whiteSpace: "pre-wrap", lineHeight: 1.65, fontSize: "var(--text-sm)" }}>{citation.text}</p>
          {citation.metadata && (
            <dl style={{ display: "grid", gridTemplateColumns: "auto 1fr", gap: "4px 12px", fontSize: "var(--text-xs)", marginTop: 16, color: "var(--ink-2)" }}>
              {Object.entries(citation.metadata).filter(([k]) => !k.startsWith("_")).map(([k, v]) => <Fragment key={k}><dt style={{ color: "var(--ink-3)" }}>{k}</dt><dd style={{ margin: 0 }}>{String(v)}</dd></Fragment>)}
            </dl>
          )}
        </>)}
      </Sheet>
    </div>
  );
}

function MobileSheet({ open, title, onClose, children }: { open: boolean; title: string; onClose: () => void; children: ReactNode }) {
  const [narrow, setNarrow] = useState(() => typeof window !== "undefined" && window.matchMedia("(max-width: 899px)").matches);
  useEffect(() => {
    const mq = window.matchMedia("(max-width: 899px)");
    const on = () => setNarrow(mq.matches);
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, []);
  return <Sheet open={open && narrow} onOpenChange={(o) => !o && onClose()} title={title}>{children}</Sheet>;
}

const AutoTextarea = forwardRef<HTMLTextAreaElement, { value: string; onChange: (v: string) => void; onEnter: () => void; placeholder: string; disabled?: boolean }>(
  function AutoTextarea({ value, onChange, onEnter, placeholder, disabled }, ref) {
    const inner = useRef<HTMLTextAreaElement | null>(null);
    useLayoutEffect(() => { const el = inner.current; if (el) { el.style.height = "auto"; el.style.height = `${Math.min(el.scrollHeight, 200)}px`; } }, [value]);
    return (
      <textarea ref={(el) => { inner.current = el; if (typeof ref === "function") ref(el); else if (ref) ref.current = el; }}
        className={s.input} rows={1} value={value} placeholder={placeholder} disabled={disabled} aria-label="Message"
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); onEnter(); } }} />
    );
  });

function ConversationList({ items, loading, activeId, onPick, muts, onDeleted }: {
  items?: Conversation[]; loading: boolean; activeId?: string; onPick: () => void;
  muts: ReturnType<typeof useConversationMutations>; onDeleted: (id: string) => void;
}) {
  const [renaming, setRenaming] = useState<Conversation | null>(null);
  const [name, setName] = useState("");
  const [deleting, setDeleting] = useState<Conversation | null>(null);
  if (loading) return <div style={{ padding: 12 }}><Spinner /></div>;
  return (
    <div className={s.convos}>
      {!items?.length && <p style={{ padding: "8px 10px", fontSize: "var(--text-sm)", color: "var(--ink-3)" }}>No conversations yet.</p>}
      {items?.map((c) => (
        <div key={c.id} className={s.convo} data-active={c.id === activeId || undefined}>
          <Link to={`/chat/${c.id}`} onClick={onPick}>
            <span className={s.convoTitle}>{c.title || "New chat"}</span>
            <span className={s.convoWhen}>{relativeTime(c.updated_at)}</span>
          </Link>
          <span className={s.convoMenu}>
            <Menu trigger={<IconButton size="sm" label={`Options for ${c.title}`}><Icon.MoreHorizontal size={16} /></IconButton>} items={[
              { label: "Rename", icon: <Icon.PenLine size={15} />, onSelect: () => { setName(c.title); setRenaming(c); } },
              { label: "Delete", danger: true, icon: <Icon.Trash2 size={15} />, onSelect: () => setDeleting(c) },
            ]} />
          </span>
        </div>
      ))}
      <Dialog open={!!renaming} onOpenChange={(o) => !o && setRenaming(null)} title="Rename chat"
        footer={<><Button variant="quiet" onClick={() => setRenaming(null)}>Cancel</Button>
          <Button variant="primary" busy={muts.rename.isPending} disabled={!name.trim()} onClick={() => muts.rename.mutate({ id: renaming!.id, title: name.trim() }, { onSuccess: () => setRenaming(null) })}>Save</Button></>}>
        <TextInput autoFocus value={name} aria-label="Title" onChange={(e) => setName(e.target.value)} />
      </Dialog>
      <ConfirmDialog open={!!deleting} onOpenChange={(o) => !o && setDeleting(null)} danger busy={muts.remove.isPending}
        title="Delete this chat?" body="The conversation and its quizzes are deleted." confirmLabel="Delete"
        onConfirm={() => muts.remove.mutate(deleting!.id, { onSuccess: () => { onDeleted(deleting!.id); setDeleting(null); } })} />
    </div>
  );
}

function ScopeControl({ scope, onChange, cfg, locked, onSaveDefault }: {
  scope: Scope; onChange: (s: Scope) => void; cfg?: ChatConfig; locked: boolean; onSaveDefault?: () => void;
}) {
  const parts = [scope.mode === "broad" ? "All material" : "Scoped", scope.grade && `Grade ${scope.grade}`, scope.subject, scope.class && `Class ${scope.class}`].filter(Boolean);
  const cols = cfg?.collections ?? [];
  return (
    <Popover align="end" trigger={<button type="button" className={s.scopeBtn} aria-label="Search scope"><Icon.Filter size={13} />{parts.join(" · ")}{locked && <Icon.Lock size={11} />}</button>}>
      <div className={s.scopeForm}>
        <div style={{ fontWeight: 600 }}>What to search</div>
        {locked && <Banner tone="info">Your school sets the scope for your class.</Banner>}
        <Segmented label="Mode" value={scope.mode} onChange={(m) => !locked && onChange({ ...scope, mode: m })}
          options={[{ value: "scoped", label: "My class's material", disabled: locked }, { value: "broad", label: "Everything", disabled: locked }]} />
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10 }}>
          <Field label="Grade"><NumberInput min={1} max={12} value={scope.grade} disabled={locked} placeholder="any" onValue={(v) => onChange({ ...scope, grade: v })} /></Field>
          <Field label="Subject"><TextInput value={scope.subject ?? ""} disabled={locked} placeholder="any" onChange={(e) => onChange({ ...scope, subject: e.target.value || null })} /></Field>
        </div>
        {cols.length > 1 && (
          <div>
            <div style={{ fontSize: "var(--text-sm)", fontWeight: 560, color: "var(--ink-2)", marginBottom: 6 }}>Collections</div>
            {cols.map((c) => {
              const on = scope.collections == null || scope.collections.includes(c);
              return (
                <label key={c} className={s.chk}>
                  <input type="checkbox" checked={on} disabled={locked} onChange={(e) => {
                    const cur = scope.collections ?? cols;
                    const next = e.target.checked ? [...cur, c] : cur.filter((x) => x !== c);
                    onChange({ ...scope, collections: next.length === cols.length ? null : next });
                  }} />{c.replace("_", " ")}
                </label>
              );
            })}
          </div>
        )}
        {onSaveDefault && !locked && <Button size="sm" onClick={onSaveDefault}>Use as this chat's default</Button>}
      </div>
    </Popover>
  );
}
