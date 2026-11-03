import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useApi, type Conversation, type Page } from "@/lib/api";
import type { ChatConfig, ConversationDetail, LearnState, LearnWidget, Scope } from "./types";

export const chatKeys = {
  config: ["chat", "config"] as const,
  list: ["chat", "conversations"] as const,
  detail: (id: string) => ["chat", "conversation", id] as const,
  artifact: (id: string) => ["chat", "artifact", id] as const,
  learning: (id: string) => ["chat", "learning", id] as const,
  memories: ["me", "memories"] as const,
  progress: ["me", "learning"] as const,
};

export interface LearningSessionView { session: LearnState | null; widget?: LearnWidget }

/** The conversation's active or paused lesson (+ the widget awaiting an answer). */
export function useLearningSession(conversationId: string | undefined) {
  const api = useApi();
  return useQuery({
    queryKey: chatKeys.learning(conversationId ?? "-"),
    enabled: !!conversationId,
    staleTime: Infinity, // turns write the fresh state into the cache as they stream
    queryFn: ({ signal }) => api.get<LearningSessionView>(`/chat/conversations/${encodeURIComponent(conversationId!)}/learning-session`, undefined, signal),
  });
}

export function useEndLesson(conversationId: string | undefined) {
  const api = useApi();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (sessionId: string) => api.post<LearnState>(`/chat/learning-sessions/${encodeURIComponent(sessionId)}/end`, {}),
    onSuccess: () => qc.setQueryData<LearningSessionView>(chatKeys.learning(conversationId ?? "-"), { session: null }),
  });
}

// ── the learner's long-term memory ("What LessonForge remembers about me") ──
export interface MemoryItemView { id: string; kind: string; content: string; created_at: string; last_used_at?: string | null; expires_at?: string | null }
export interface MasteryView { kc_ref: string; title: string; mastery: number; attempts: number; correct: number; due: boolean; due_at: string }

export function useMemories(enabled: boolean) {
  const api = useApi();
  return useQuery({ queryKey: chatKeys.memories, enabled, queryFn: ({ signal }) => api.get<{ enabled: boolean; items: MemoryItemView[] }>("/me/memories", undefined, signal) });
}

export function useProgress(enabled: boolean) {
  const api = useApi();
  return useQuery({ queryKey: chatKeys.progress, enabled, queryFn: ({ signal }) => api.get<{ items: MasteryView[]; due: MasteryView[] }>("/me/learning", undefined, signal) });
}

export function useMemoryMutations() {
  const api = useApi();
  const qc = useQueryClient();
  const refresh = () => { void qc.invalidateQueries({ queryKey: chatKeys.memories }); void qc.invalidateQueries({ queryKey: chatKeys.progress }); };
  return {
    forget: useMutation({ mutationFn: (id: string) => api.request("DELETE", `/me/memories/${encodeURIComponent(id)}`), onSuccess: refresh }),
    forgetAll: useMutation({ mutationFn: () => api.request("DELETE", "/me/memories"), onSuccess: refresh }),
    setEnabled: useMutation({ mutationFn: (enabled: boolean) => api.request("PUT", "/me/memory-settings", { body: { enabled } }), onSuccess: refresh }),
  };
}

export function useChatConfig() {
  const api = useApi();
  return useQuery({ queryKey: chatKeys.config, staleTime: Infinity, queryFn: ({ signal }) => api.get<ChatConfig>("/chat/config", undefined, signal) });
}

export function useConversations() {
  const api = useApi();
  return useQuery({
    queryKey: chatKeys.list,
    queryFn: ({ signal }) => api.get<Page<Conversation>>("/chat/conversations", { limit: 100 }, signal),
    select: (p) => p.items,
  });
}

export function useConversation(id: string | undefined) {
  const api = useApi();
  return useQuery({
    queryKey: chatKeys.detail(id ?? "-"),
    enabled: !!id,
    queryFn: ({ signal }) => api.get<ConversationDetail>(`/chat/conversations/${encodeURIComponent(id!)}`, undefined, signal),
    staleTime: Infinity, // the open thread is the source of truth while streaming
  });
}

export const scopeToDefaults = (s: Scope) => ({ mode: s.mode, grade: s.grade, subject: s.subject, class: s.class, collections: s.collections });

export function useConversationMutations() {
  const api = useApi();
  const qc = useQueryClient();
  const refreshList = () => qc.invalidateQueries({ queryKey: chatKeys.list });
  return {
    create: useMutation({
      mutationFn: (scope: Scope) => api.post<Conversation>("/chat/conversations", { title: "New chat", defaults: scopeToDefaults(scope) }),
      onSuccess: refreshList,
    }),
    rename: useMutation({
      mutationFn: ({ id, title }: { id: string; title: string }) => api.request<Conversation>("PATCH", `/chat/conversations/${encodeURIComponent(id)}`, { body: { title } }),
      onSuccess: refreshList,
    }),
    saveScope: useMutation({
      mutationFn: ({ id, scope }: { id: string; scope: Scope }) => api.request<Conversation>("PATCH", `/chat/conversations/${encodeURIComponent(id)}`, { body: { defaults: scopeToDefaults(scope) } }),
    }),
    remove: useMutation({
      mutationFn: (id: string) => api.request("DELETE", `/chat/conversations/${encodeURIComponent(id)}`),
      onSuccess: refreshList,
    }),
  };
}
