import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { describeError, useApi } from "@/lib/api";
import { dateTime, formatScore, humanize, plural } from "@/lib/format";
import { Badge, Banner, Button, ConfirmDialog, ErrorState, Icon, Menu, RichText, SkeletonLines, useToast } from "@/ui";
import { chatKeys } from "../../api";
import type { ArtifactSummary } from "../../types";
import type { ArtifactRendererProps } from "../registry";
import type { ArtifactDetail, Graded, Review } from "./types";
import { WIDGETS } from "./widgets";
import s from "./quiz.module.css";

type View = { name: "intro" } | { name: "play"; attemptId: string; idx: number; results: Record<string, Graded> } | { name: "review"; review: Review; fresh: boolean };

/** The interactive quiz: graded on the server, one question at a time, with history and retry. */
export function QuizArtifact({ id, onSummary, onNew, onDeleted }: ArtifactRendererProps) {
  const api = useApi();
  const qc = useQueryClient();
  const toast = useToast();
  const q = useQuery({ queryKey: chatKeys.artifact(id), queryFn: ({ signal }) => api.get<ArtifactDetail>(`/chat/artifacts/${encodeURIComponent(id)}`, undefined, signal) });
  const [view, setView] = useState<View>({ name: "intro" });
  const [busy, setBusy] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);

  const refresh = async () => {
    const d = await qc.fetchQuery({ queryKey: chatKeys.artifact(id), queryFn: () => api.get<ArtifactDetail>(`/chat/artifacts/${encodeURIComponent(id)}`), staleTime: 0 });
    onSummary(d.summary);
  };
  const run = async <T,>(fn: () => Promise<T>): Promise<T | undefined> => {
    setBusy(true);
    try { return await fn(); } catch (e) { toast.error(describeError(e)); return undefined; } finally { setBusy(false); }
  };

  if (q.isPending) return <SkeletonLines lines={5} />;
  if (q.error) return <ErrorState error={describeError(q.error)} onRetry={() => q.refetch()} />;
  const { play, summary, attempts } = q.data;
  const questions = play.questions;

  const start = () => run(async () => {
    const r = await api.post<{ attempt_id: string }>(`/chat/artifacts/${encodeURIComponent(id)}/attempts`);
    setView({ name: "play", attemptId: r.attempt_id, idx: 0, results: {} });
  });
  const resume = (attemptId: string) => run(async () => {
    const rv = await api.get<Review>(`/chat/attempts/${encodeURIComponent(attemptId)}`);
    const results: Record<string, Graded> = {};
    rv.questions.forEach((x) => { if (x.answered) results[x.id] = { correct: (x.score ?? 0) >= 1, score: x.score ?? 0, expected: x.expected ?? "", explanation: x.explanation, response: x.response }; });
    const next = questions.findIndex((x) => !results[x.id]);
    setView({ name: "play", attemptId, idx: next < 0 ? questions.length - 1 : next, results });
  });
  const finish = (attemptId: string) => run(async () => {
    const rv = await api.post<Review>(`/chat/attempts/${encodeURIComponent(attemptId)}/finish`);
    await refresh();
    setView({ name: "review", review: rv, fresh: true });
  });
  const retryMissed = (attemptId: string) => run(async () => {
    const sum = await api.post<ArtifactSummary>(`/chat/attempts/${encodeURIComponent(attemptId)}/retry-missed`);
    onNew(sum);
  });
  const exportAs = (fmt: string) => run(async () => {
    const name = await api.download("POST", `/chat/artifacts/${encodeURIComponent(id)}/export`, { query: { fmt }, fallbackName: `quiz.${fmt}` });
    toast.show(`Downloaded ${name}`);
  });

  if (view.name === "play") {
    const cur = questions[view.idx]!;
    const result = view.results[cur.id] ?? null;
    const answered = Object.values(view.results).filter((r) => !r.pending).length;
    const last = view.idx === questions.length - 1;
    const Widget = WIDGETS[cur.widget] ?? WIDGETS.text!;
    const submit = (response: unknown, selfMark?: boolean) => run(async () => {
      const g = await api.post<Graded>(`/chat/attempts/${encodeURIComponent(view.attemptId)}/answer`, { question_id: cur.id, response, self_mark: selfMark ?? null });
      setView({ ...view, results: { ...view.results, [cur.id]: { ...g, response: response ?? view.results[cur.id]?.response } } });
    });
    return (
      <div className={s.quiz}>
        <div className={s.progress}>
          <span>Question {view.idx + 1} of {questions.length}</span>
          <div className={s.bar} role="progressbar" aria-valuenow={answered} aria-valuemax={questions.length}><i style={{ width: `${(answered / questions.length) * 100}%` }} /></div>
          <Button size="sm" variant="quiet" onClick={() => { void refresh(); setView({ name: "intro" }); }}>Exit</Button>
        </div>
        <div className={s.prompt}><RichText text={cur.prompt} /></div>
        <Widget key={cur.id} q={cur} result={result && !result.pending ? result : null} previous={result?.response} busy={busy} submit={(r) => void submit(r)} />
        {result?.pending && (
          <div className={`${s.fb} ${s.part}`}>
            <div className={s.fbHead}>Compare with the model answer</div>
            <div><b>Model answer:</b> <RichText text={result.expected} /></div>
            {!!result.key_points?.length && <ul>{result.key_points.map((k, i) => <li key={i}>{k}</li>)}</ul>}
            <div className={s.row}>
              <Button variant="primary" busy={busy} onClick={() => void submit(null, true)}>I got it</Button>
              <Button busy={busy} onClick={() => void submit(null, false)}>Not quite</Button>
            </div>
          </div>
        )}
        {result && !result.pending && (
          <div className={`${s.fb} ${result.correct ? s.ok : result.score > 0 ? s.part : s.no}`} role="status">
            <div className={s.fbHead}>{result.correct ? "Correct" : result.score > 0 ? "Partly right" : "Not quite"}</div>
            {result.feedback && <p>{result.feedback}</p>}
            {!result.correct && <div><b>Answer:</b> <RichText text={result.expected} /></div>}
            {result.explanation && <RichText text={result.explanation} />}
            <div className={s.row}>
              {last ? <Button variant="primary" busy={busy} onClick={() => void finish(view.attemptId)}>See results</Button>
                : <Button variant="primary" icon={<Icon.ArrowRight size={16} />} onClick={() => setView({ ...view, idx: view.idx + 1 })}>Next</Button>}
            </div>
          </div>
        )}
        {!result && <div className={s.row} style={{ justifyContent: "flex-end" }}>
          {view.idx > 0 && <Button size="sm" variant="quiet" onClick={() => setView({ ...view, idx: view.idx - 1 })}>Back</Button>}
          <Button size="sm" variant="quiet" onClick={() => (last ? void finish(view.attemptId) : setView({ ...view, idx: view.idx + 1 }))}>{last ? "Finish now" : "Skip"}</Button>
        </div>}
      </div>
    );
  }

  if (view.name === "review") {
    const { review: rv, fresh } = view;
    const p = rv.attempt.percent;
    const msg = p === 100 ? "Perfect score." : p >= 80 ? "Great work." : p >= 50 ? "Good effort — a little more practice." : "Keep going — review the ones you missed.";
    return (
      <div className={s.quiz}>
        <h3 className={s.title}>{fresh ? "Quiz complete" : `Attempt · ${dateTime(rv.attempt.created_at)}`}</h3>
        <div className={s.score}>
          <div className={s.ring} style={{ ["--p" as string]: p }}><b>{p}%</b></div>
          <div><div className={s.big}>{formatScore(rv.attempt.score)} / {rv.attempt.total}</div><div className={s.muted}>{rv.attempt.status === "completed" ? msg : "Not finished"}</div></div>
        </div>
        {Object.keys(rv.by_type).length > 1 && (
          <div className={s.byType}>
            {Object.entries(rv.by_type).map(([t, v]) => (
              <div key={t} className={s.byRow}><span>{humanize(t)}</span><div className={s.bar}><i style={{ width: `${(v.score / v.of) * 100}%` }} /></div><span>{formatScore(v.score)}/{v.of}</span></div>
            ))}
          </div>
        )}
        <div className={s.reviewList}>
          {rv.questions.map((x) => {
            const ok = x.answered && (x.score ?? 0) >= 1;
            return (
              <details key={x.id} className={s.rv}>
                <summary><span className={ok ? s.markOk : s.markNo}>{ok ? <Icon.Check size={13} /> : <Icon.X size={13} />}</span>{x.number}. {x.prompt.slice(0, 120)}</summary>
                <div className={s.rvBody}>
                  {!x.answered ? <p className={s.muted}>Not answered.</p> : (<>
                    {x.response != null && x.response !== "" && <p className={s.muted}>You: {Array.isArray(x.response) ? x.response.join(", ") : String(x.response)}</p>}
                    <div><b>Answer:</b> <RichText text={x.expected ?? ""} /></div>
                    {x.explanation && <RichText text={x.explanation} />}
                  </>)}
                </div>
              </details>
            );
          })}
        </div>
        <div className={s.row}>
          {rv.missed.length > 0 && <Button variant="primary" busy={busy} onClick={() => void retryMissed(rv.attempt.id)}>Retry the {rv.missed.length} missed</Button>}
          <Button busy={busy} variant={rv.missed.length ? "default" : "primary"} onClick={() => void start()}>Play again</Button>
          <Button variant="quiet" onClick={() => setView({ name: "intro" })}>Back</Button>
        </div>
      </div>
    );
  }

  const kinds = Object.entries(questions.reduce<Record<string, number>>((a, x) => ({ ...a, [x.type]: (a[x.type] ?? 0) + 1 }), {}));
  return (
    <div className={s.quiz}>
      <h3 className={s.title}>{play.title}</h3>
      <p className={s.muted}>{plural(questions.length, "question")} · each answer is checked and explained as you go.</p>
      <div className={s.row} style={{ gap: 6 }}>{kinds.map(([t, n]) => <Badge key={t}>{n} {humanize(t)}</Badge>)}</div>
      {play.notes.map((n, i) => <Banner key={i} tone="info">{n}</Banner>)}
      <div className={s.row}>
        {summary.in_progress && <Button variant="primary" busy={busy} onClick={() => void resume(summary.in_progress!)}>Resume</Button>}
        <Button variant={summary.in_progress ? "default" : "primary"} busy={busy} onClick={() => void start()}>{summary.in_progress ? "Start over" : "Start the quiz"}</Button>
        <Menu trigger={<Button variant="quiet" icon={<Icon.Download size={15} />}>Download</Button>}
          items={["docx", "pptx", "md"].map((f) => ({ label: `.${f}`, onSelect: () => void exportAs(f) }))} />
        <Button variant="quiet" icon={<Icon.Trash2 size={15} />} onClick={() => setConfirmDelete(true)} aria-label="Delete quiz" />
      </div>
      {attempts.length > 0 && (
        <div>
          <div className={s.sect}>History</div>
          {attempts.map((a) => (
            <button key={a.id} type="button" className={s.att} onClick={() => void run(async () => setView({ name: "review", review: await api.get<Review>(`/chat/attempts/${encodeURIComponent(a.id)}`), fresh: false }))}>
              <span>{dateTime(a.created_at)}</span>
              <span className={s.muted}>{a.status === "completed" ? "completed" : `${a.answered}/${a.total} answered`}</span>
              <b>{formatScore(a.score)} / {a.total}</b>
            </button>
          ))}
        </div>
      )}
      {play.sources.length > 0 && <p className={s.muted} style={{ fontSize: "var(--text-xs)" }}>Based on: {play.sources.join(" · ")}</p>}
      <ConfirmDialog open={confirmDelete} onOpenChange={setConfirmDelete} danger busy={busy} title="Delete this quiz?" confirmLabel="Delete"
        body="The quiz and its attempt history are removed." onConfirm={() => void run(async () => {
          await api.request("DELETE", `/chat/artifacts/${encodeURIComponent(id)}`);
          setConfirmDelete(false); onDeleted(id);
        })} />
    </div>
  );
}
