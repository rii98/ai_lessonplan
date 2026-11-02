import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useApi, type Conversation, type Page } from "@/lib/api";
import type { ChatConfig, ConversationDetail, Scope } from "./types";

export const chatKeys = {
  config: ["chat", "config"] as const,
  list: ["chat", "conversations"] as const,
  detail: (id: string) => ["chat", "conversation", id] as const,
  artifact: (id: string) => ["chat", "artifact", id] as const,
};

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
