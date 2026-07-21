"use client";

/**
 * useVaultReview — data layer for the Vault-AI Review tab.
 *
 * Contract: plans/VAULT_AI_ACTIONS_CONTRACT.md §2.
 *   GET  /admin/vault-ai/review              → { folder:[], ppv:[], reminder:[] }
 *   POST /admin/vault-ai/review/approve      → { approved:[id], stale:[id] }
 *   POST /admin/vault-ai/review/reject       → { rejected:[id] }
 *
 * Approve accepts EITHER `{ kind }` (section approve = every pending row of
 * that kind) OR `{ ids }` (per-item). Reject is ids-only. Stale ids are
 * returned by the server when baseline_json no longer matches — they stay
 * `pending` so the caller can badge them and re-review.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { relay } from "@/lib/relay";

export type ReviewKind = "folder" | "ppv" | "reminder";

export interface ReviewItem {
  id: number;
  kind: ReviewKind;
  status: string;
  payload: Record<string, unknown>;
  created_at: string;
}

export interface ReviewResp {
  folder: ReviewItem[];
  ppv: ReviewItem[];
  reminder: ReviewItem[];
}

export interface ApproveResp {
  approved: number[];
  stale: number[];
}

export interface RejectResp {
  rejected: number[];
}

export function reviewQueryKey(accountId: string | null) {
  return ["vault-ai-review", accountId] as const;
}

export function useVaultReview(accountId: string | null) {
  return useQuery<ReviewResp>({
    queryKey: reviewQueryKey(accountId),
    enabled: !!accountId,
    queryFn: () =>
      relay.get<ReviewResp>(
        `/admin/vault-ai/review?account_id=${encodeURIComponent(accountId!)}`,
        { accountId },
      ),
    staleTime: 15_000,
    refetchOnWindowFocus: false,
  });
}

export function useApproveSection(accountId: string | null) {
  const qc = useQueryClient();
  return useMutation<ApproveResp, Error, ReviewKind>({
    mutationFn: (kind) =>
      relay.post<ApproveResp>(
        `/admin/vault-ai/review/approve`,
        { account_id: accountId, kind },
        { accountId },
      ),
    onSuccess: () => qc.invalidateQueries({ queryKey: reviewQueryKey(accountId) }),
  });
}

export function useApproveIds(accountId: string | null) {
  const qc = useQueryClient();
  return useMutation<ApproveResp, Error, number[]>({
    mutationFn: (ids) =>
      relay.post<ApproveResp>(
        `/admin/vault-ai/review/approve`,
        { account_id: accountId, ids },
        { accountId },
      ),
    onSuccess: () => qc.invalidateQueries({ queryKey: reviewQueryKey(accountId) }),
  });
}

export function useRejectIds(accountId: string | null) {
  const qc = useQueryClient();
  return useMutation<RejectResp, Error, number[]>({
    mutationFn: (ids) =>
      relay.post<RejectResp>(
        `/admin/vault-ai/review/reject`,
        { account_id: accountId, ids },
        { accountId },
      ),
    onSuccess: () => qc.invalidateQueries({ queryKey: reviewQueryKey(accountId) }),
  });
}
