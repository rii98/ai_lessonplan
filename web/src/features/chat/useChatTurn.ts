import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useRef, useState } from "react";
import { describeError, isAbort, useApi } from "@/lib/api";
import { chatKeys } from "./api";
import { applyEvent, emptyTurn, type Turn } from "./turn";
import type { ConversationDetail, Scope } from "./types";

/**
 * Send one message and stream the reply. Repaints are coalesced to one per
 * animation frame — Markdown + math rendering is heavier than the token rate.
 * `stop()` aborts the request; what arrived so far is kept.
 */
export function useChatTurn() {
  const api = useApi();
  const qc = useQueryClient();
  const [turn, setTurn] = useState<(Turn & { userText: string; conversationId: string }) | null>(null);
  const abort = useRef<AbortController | null>(null);

  const send = useCallback(async (conversationId: string, text: string, scope: Scope) => {
    abort.current?.abort();
    const ctl = new AbortController();
    abort.current = ctl;
    let t = emptyTurn();
    let frame = 0;
    const flush = () => { frame = 0; setTurn({ ...t, userText: text, conversationId }); };
    flush();
    const filters: Record<string, unknown> = {};
    if (scope.grade != null) filters.grade = scope.grade;
    if (scope.subject) filters.subject = scope.subject;
    if (scope.class) filters.class = scope.class;
    try {
      for await (const ev of api.stream(`/chat/conversations/${encodeURIComponent(conversationId)}/message`,
        { message: text, mode: scope.mode, filters }, ctl.signal)) {
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
      qc.setQueryData<ConversationDetail>(chatKeys.detail(conversationId), (prev) => prev && ({
        ...prev,
        conversation: t.title ? { ...prev.conversation, title: t.title } : prev.conversation,
        messages: [...prev.messages, { role: "user", content: text },
          ...(t.content || t.artifacts.length ? [{ role: "assistant" as const, content: t.content, citations: t.citations,
            meta: { artifact_ids: t.artifacts.map((a) => a.id), intent: t.skill ? { skill: t.skill } : null } }] : [])],
        artifacts: [...prev.artifacts, ...t.artifacts.filter((a) => !prev.artifacts.some((p) => p.id === a.id))],
      }));
      void qc.invalidateQueries({ queryKey: chatKeys.list });
      abort.current = null;
    }
    return t;
  }, [api, qc]);

  const stop = useCallback(() => abort.current?.abort(), []);
  const clear = useCallback(() => setTurn(null), []);
  return { turn, streaming: !!turn && !turn.done, send, stop, clear };
}
