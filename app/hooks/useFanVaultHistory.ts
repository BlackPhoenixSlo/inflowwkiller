"use client";

/**
 * useFanVaultHistory — per-fan vault send/purchase history.
 *
 * Drives the VaultPicker's status badges ("sent · purchased · unseen")
 * and any future "show me what she's already paid for" filters. Backed
 * by the local SQLite tables `vault_sends` + `transactions` — shared
 * across all chatters on the same model account.
 *
 * Cache strategy:
 *   • Keyed on (accountId, fanId) so popping between chats is instant.
 *   • staleTime 60s — vault history rarely changes mid-session.
 *   • Persisted via the global PersistQueryClient layer (the `fan` /
 *     `vault-history` keys are whitelisted in providers.tsx).
 */

import { useQuery, useQueryClient } from "@tanstack/react-query";

import { relay } from "@/lib/relay";
import { type FanId } from "@/lib/fanId";

export interface FanVaultEntry {
  send_count: number;
  last_sent_at: string | null;
  last_price_cents: number;
  was_purchased: boolean;
  last_purchase_at: string | null;
  total_paid_cents: number;
}

export interface FanVaultHistoryResp {
  by_media: Record<string, FanVaultEntry>;
}

export function useFanVaultHistory(
  accountId: string | null,
  fanId: FanId | null,
  enabled = true,
) {
  return useQuery<FanVaultHistoryResp>({
    queryKey: ["vault-history", accountId, fanId],
    enabled: enabled && !!accountId && fanId != null,
    queryFn: async () => {
      const qs = new URLSearchParams({
        account_id: accountId!,
        fan_id: String(fanId),
      });
      return relay.get<FanVaultHistoryResp>(`/admin/vault/fan-history?${qs.toString()}`);
    },
    staleTime: 60_000,
    refetchOnWindowFocus: false,
  });
}

/** Invalidate the vault-history cache for one (account, fan) pair. Call
 *  after a successful chat-send with attached media so the picker's
 *  badges update on next open. */
export function invalidateFanVaultHistory(
  qc: ReturnType<typeof useQueryClient>,
  accountId: string,
  fanId: FanId,
): void {
  qc.invalidateQueries({ queryKey: ["vault-history", accountId, fanId] });
}
