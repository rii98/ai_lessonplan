import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useRef, useState } from "react";
import { describeError, isAbort, useApi } from "@/lib/api";
import { chatKeys, type LearningSessionView } from "./api";
import { applyEvent, emptyTurn, type Turn } from "./turn";
import type { ChatMessage, ConversationDetail, LearnAction, Scope } from "./types";

/** What one turn sends. A typed message, a learn-mode input (widget answer / quick
 *  action), or the start of a lesson — all stream the same SSE turn protocol. */
export type TurnInput =
  | { kind: "message"; text: string }
  | { kind: "learn"; label: string; action?: LearnAction; value?: string; widget?: { widget_id: string; response: unknown; confidence?: number } }
  | { kind: "start"; topic: string };

const label = (i: TurnInput) => (i.kind === "message" ? i.text : i.kind === "start" ? `Teach me: ${i.topic}` : i.label);

function filtersOf(scope: Scope): Record<string, unknown> {
  const f: Record<string, unknown> = {};
  if (scope.grade != null) f.grade = scope.grade;
  if (scope.subject) f.subject = scope.subject;
  if (scope.class) f.class = scope.class;
  return f;
}

/**
 * Send one turn and stream the reply. Repaints are coalesced to one per animation
 * frame — Markdown + math rendering is heavier than the token rate. `stop()` aborts
 * the request; what arrived so far is kept.
 */
export function useChatTurn() {
  const api = useApi();
  const qc = useQueryClient();
  const [turn, setTurn] = useState<(Turn & { userText: string; conversationId: string }) | null>(null);
  const abort = useRef<AbortController | null>(null);

  const run = useCallback(async (conversationId: string, input: TurnInput, scope: Scope) => {
    abort.current?.abort();
    const ctl = new AbortController();
    abort.current = ctl;
    const userText = label(input);
    let t = emptyTurn();
    let frame = 0;
    const flush = () => { frame = 0; setTurn({ ...t, userText, conversationId }); };
    flush();
    const base = `/chat/conversations/${encodeURIComponent(conversationId)}`;
    const [path, body] = input.kind === "message"
      ? [`${base}/message`, { message: input.text, mode: scope.mode, filters: filtersOf(scope) }]
      : input.kind === "start"
        ? [`${base}/learning-sessions`, { topic: input.topic, mode: scope.mode, filters: filtersOf(scope) }]
        : [`${base}/learn`, input.widget ? { widget_response: input.widget } : { action: input.action, action_value: input.value }];
    try {
      for await (const ev of api.stream(path, body, ctl.signal)) {
        t = applyEvent(t, ev);
        if (ev.event === "token") { if (!frame) frame = requestAnimationFrame(flush); }
        else flush();
      }
      if (!t.done) t = { ...t, done: true };
    } catch (e) {
      t = { ...t, done: true, error: isAbort(e) ? null : describeError(e), status: null };
    } finally {
      if (frame) cancelAnimationFrame(frame);
      flush();
      // persist into the cached thread, then let the live turn go
      const reply: ChatMessage[] = t.content || t.artifacts.length || t.widget ? [{
        role: "assistant", content: t.content, citations: t.citations,
        meta: {
          artifact_ids: t.artifacts.map((a) => a.id), intent: t.skill ? { skill: t.skill } : null,
          ...(t.learn ? { learn: { widget_id: t.widget?.id ?? null } } : {}),
          ...(t.widget ? { widget: t.widget } : {}), ...(t.offer ? { offer: t.offer } : {}),
        },
      }] : [];
      qc.setQueryData<ConversationDetail>(chatKeys.detail(conversationId), (prev) => prev && ({
        ...prev,
        conversation: t.title ? { ...prev.conversation, title: t.title } : prev.conversation,
        messages: [...prev.messages, { role: "user", content: userText, meta: t.feedback ? { feedback: t.feedback } : undefined }, ...reply],
        artifacts: [...prev.artifacts, ...t.artifacts.filter((a) => !prev.artifacts.some((p) => p.id === a.id))],
      }));
      if (t.learn) {
        const ended = t.learn.status === "completed" || t.learn.status === "ended";
        qc.setQueryData<LearningSessionView>(chatKeys.learning(conversationId), ended ? { session: null }
          : { session: t.learn, widget: t.widget ?? undefined });
        if (ended) void qc.invalidateQueries({ queryKey: chatKeys.progress });
      }
      void qc.invalidateQueries({ queryKey: chatKeys.list });
      abort.current = null;
    }
    return t;
  }, [api, qc]);

  /** Back-compat: send a typed message. */
  const send = useCallback((conversationId: string, text: string, scope: Scope) =>
    run(conversationId, { kind: "message", text }, scope), [run]);

  const stop = useCallback(() => abort.current?.abort(), []);
  const clear = useCallback(() => setTurn(null), []);
  return { turn, streaming: !!turn && !turn.done, send, run, stop, clear };
}
