import { useState } from "react";
import { Banner, Button, ConfirmDialog, Dialog, Icon, IconButton, Meter, SkeletonLines, TextInput } from "@/ui";
import { describeError } from "@/lib/api";
import { useMemories, useMemoryMutations, useProgress } from "../api";
import s from "./learn.module.css";

/**
 * "What LessonForge remembers about me" — the learner sees, deletes, or switches off
 * their long-term memory, and sees their progress (mastery fades until reviewed).
 * Memory only ever personalises; it is never used to decide what someone may do.
 */
export function MemoryDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (o: boolean) => void }) {
  const mem = useMemories(open);
  const progress = useProgress(open);
  const m = useMemoryMutations();
  const [clearing, setClearing] = useState(false);
  const [draft, setDraft] = useState("");
  const addNote = () => {
    const content = draft.trim();
    if (content.length < 3) return;
    m.add.mutate(content, { onSuccess: () => setDraft("") });
  };
  const enabled = mem.data?.enabled ?? true;
  return (
    <Dialog open={open} onOpenChange={onOpenChange} title="What LessonForge remembers"
      description="A few notes so lessons fit you, and what you've mastered. Only you see this."
      footer={<>
        <Button variant="quiet" onClick={() => m.setEnabled.mutate(!enabled)} busy={m.setEnabled.isPending}>{enabled ? "Turn memory off" : "Turn memory on"}</Button>
        <Button variant="danger" disabled={!mem.data?.items.length && !progress.data?.items.length} onClick={() => setClearing(true)}>Forget everything</Button>
      </>}>
      {mem.error && <Banner tone="danger">{describeError(mem.error)}</Banner>}
      {!enabled && <Banner tone="info">Memory is off — nothing new is remembered, and lessons won't use what's here.</Banner>}
      <h4 style={{ margin: "4px 0 8px" }}>Notes</h4>
      {enabled && (
        <form className={s.memAdd} onSubmit={(e) => { e.preventDefault(); addNote(); }}>
          <TextInput value={draft} maxLength={200} onChange={(e) => setDraft(e.target.value)}
            placeholder="Tell me something to remember, e.g. “use cricket examples”" aria-label="Add a note" />
          <Button type="submit" busy={m.add.isPending} disabled={draft.trim().length < 3}>Remember</Button>
        </form>
      )}
      {m.add.error && <Banner tone="danger">{describeError(m.add.error)}</Banner>}
      {mem.isPending ? <SkeletonLines lines={3} /> : !mem.data?.items.length
        ? <p className={s.muted} style={{ fontSize: "var(--text-sm)" }}>Nothing yet. Tell me how you like to learn (“use football examples”) and I'll remember.</p>
        : (
          <ul className={s.memList}>
            {mem.data.items.map((i) => (
              <li key={i.id} className={s.memItem}>
                <span><div>{i.content}</div><div className={s.memKind}>{i.kind.replace("_", " ")}{i.origin === "you" ? " · you asked" : ""}</div></span>
                <IconButton size="sm" label={`Forget “${i.content}”`} onClick={() => m.forget.mutate(i.id)}><Icon.Trash2 size={15} /></IconButton>
              </li>
            ))}
          </ul>
        )}
      <h4 style={{ margin: "16px 0 8px" }}>Progress</h4>
      {progress.isPending ? <SkeletonLines lines={2} /> : !progress.data?.items.length
        ? <p className={s.muted} style={{ fontSize: "var(--text-sm)" }}>Finish a lesson in Learn mode to see your progress here.</p>
        : (
          <ul className={s.memList}>
            {progress.data.items.map((r) => (
              <li key={r.kc_ref} className={s.memItem}>
                <span><div>{r.title || r.kc_ref}</div><Meter value={r.mastery} label={`${Math.round(r.mastery * 100)}% mastered`} /></span>
                {r.due && <span className={s.memKind}>review due</span>}
              </li>
            ))}
          </ul>
        )}
      <ConfirmDialog open={clearing} onOpenChange={setClearing} danger busy={m.forgetAll.isPending} title="Forget everything?"
        body="All notes and your progress history are deleted. Lessons start fresh." confirmLabel="Forget everything"
        onConfirm={() => m.forgetAll.mutate(undefined, { onSuccess: () => setClearing(false) })} />
    </Dialog>
  );
}
