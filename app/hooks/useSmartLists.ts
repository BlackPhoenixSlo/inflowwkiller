"use client";

/**
 * useSmartLists — data layer for Smart Lists (saved dynamic fan segments).
 *
 * Reads/writes smart_lists via the relay's /admin/smart-lists/* routes. A
 * Smart List is a named rules object (`{match, rules[]}`) resolved server-side
 * into a fan-id set — reused as a mass-message audience (include/exclude) and
 * previewed live in the builder.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { relay } from "@/lib/relay";

const KEY = "smart-lists";

export type SmartField =
  | "lifetime_spend" | "recent_spend" | "last_active_days" | "status" | "tag";
export type SmartOp = ">" | ">=" | "<" | "<=" | "==" | "!=" | "has" | "not_has";

export interface SmartRule {
  field: SmartField;
  op: SmartOp;
  value: number | string;
  days?: number;         // recent_spend window
}

export interface SmartRules {
  match: "all" | "any";
  rules: SmartRule[];
}

export interface SmartList {
  id: number;
  account_id: string;
  name: string;
  rules: SmartRules;
  created_at: string | null;
  updated_at: string | null;
}

export interface SmartPreview {
  count: number;
  total_fans: number;
  sample: Array<{
    fan_id: number;
    name: string;
    lifetime_spend_cents: number;
    last_active_days: number | null;
    status: string | null;
  }>;
}

export function useSmartLists(accountId: string | null) {
  return useQuery<SmartList[]>({
    queryKey: [KEY, accountId],
    enabled: !!accountId,
    staleTime: 30_000,
    queryFn: async () => {
      const r = await relay.get<{ lists: SmartList[] }>(
        `/admin/smart-lists?account_id=${encodeURIComponent(accountId!)}`,
      );
      return r.lists ?? [];
    },
  });
}

export function useCreateSmartList(accountId: string | null) {
  const qc = useQueryClient();
  return useMutation<SmartList, Error, { name: string; rules: SmartRules }>({
    mutationFn: (body) =>
      relay.post<SmartList>("/admin/smart-lists", { account_id: accountId, ...body }),
    onSuccess: () => qc.invalidateQueries({ queryKey: [KEY, accountId] }),
  });
}

export function useUpdateSmartList(accountId: string | null) {
  const qc = useQueryClient();
  return useMutation<SmartList, Error, { id: number; name?: string; rules?: SmartRules }>({
    mutationFn: ({ id, ...patch }) =>
      relay.patch<SmartList>(`/admin/smart-lists/${id}`, patch),
    onSuccess: () => qc.invalidateQueries({ queryKey: [KEY, accountId] }),
  });
}

export function useDeleteSmartList(accountId: string | null) {
  const qc = useQueryClient();
  return useMutation<unknown, Error, number>({
    mutationFn: (id) => relay.delete(`/admin/smart-lists/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: [KEY, accountId] }),
  });
}

export function usePreviewSmartList(accountId: string | null) {
  return useMutation<SmartPreview, Error, SmartRules>({
    mutationFn: (rules) =>
      relay.post<SmartPreview>("/admin/smart-lists/preview", {
        account_id: accountId, rules,
      }),
  });
}

/** Expand a saved list to its current matching fan-id set — the mass composer
 *  calls this to turn an include/exclude chip into explicit userIds. */
export async function resolveSmartList(id: number): Promise<number[]> {
  const r = await relay.get<{ fan_ids: number[] }>(`/admin/smart-lists/${id}/resolve`);
  return r.fan_ids ?? [];
}
