"use client";

/**
 * useFanLines — a fan's saved conversation openers (gen_info's Q1-3 + Tease1-3),
 * for the composer's "Lines" picker. `consume(slot)` deletes one used line so it's
 * never offered twice (called after the message is actually sent).
 *
 * GET    /admin/fans/{account}/{fan}/lines          → { questions, teases }
 * DELETE /admin/fans/{account}/{fan}/lines/{slot}   → consume one line
 * POST   /admin/fans/{account}/{fan}/lines/generate → force a fresh batch (gen_info)
 *
 * The query is cheap (local SQLite) but only fetched when `enabled` (the picker is
 * open), so it doesn't fire for every chat. Pass enabled=false to get just the
 * `consume`/`generate` actions (e.g. from the composer's send path).
 */

import { useQuery, useQueryClient } from "@tanstack/react-query";

import { relay } from "@/lib/relay";

export interface FanLine {
  /** one of q1|q2|q3|tease1|tease2|tease3 — the column to consume on send. */
  slot: string;
  text: string;
}

export interface FanLines {
  questions: FanLine[];
  teases: FanLine[];
}

export function useFanLines(
  accountId: string | null,
  fanId: number | null,
  enabled: boolean,
) {
  const qc = useQueryClient();
  const query = useQuery<FanLines>({
    queryKey: ["fan-lines", accountId, fanId],
    enabled: enabled && !!accountId && !!fanId,
    queryFn: () =>
      relay.get<FanLines>(`/admin/fans/${accountId}/${fanId}/lines`),
    staleTime: 60_000,
  });

  async function consume(slot: string): Promise<void> {
    if (!accountId || !fanId || !slot) return;
    try {
      await relay.delete(`/admin/fans/${accountId}/${fanId}/lines/${slot}`);
    } catch {
      // best-effort: a failed consume just means the line may show again — never
      // block the send on it.
    } finally {
      void qc.invalidateQueries({ queryKey: ["fan-lines", accountId, fanId] });
    }
  }

  /**
   * Force a fresh batch (gen_info force_ids) for this fan. The new lines land
   * asynchronously — the executor claims the job on its next ~30s tick — so the
   * caller should poll/refetch the query until the batch changes. Resolves once
   * the job is ENQUEUED, not once the lines exist.
   */
  async function generate(): Promise<void> {
    if (!accountId || !fanId) return;
    await relay.post(`/admin/fans/${accountId}/${fanId}/lines/generate`);
  }

  return { ...query, consume, generate };
}
