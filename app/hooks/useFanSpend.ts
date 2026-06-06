"use client";

/**
 * useFanSpend — bulk lifetime-spend lookup for ChatList row chips.
 *
 * Reads from our local SQLite (`/admin/fans/{aid}/spend-batch`), which the
 * WS pump + event-transcoder keep within seconds of OF for any active fan.
 * This is dramatically faster than the old `/users/list?view=x` round-trip
 * (one SQL query vs N OF API calls) and survives a tab close because the
 * query is persisted by providers.tsx.
 *
 * If a fan has never been touched by the pump (cold-start, brand new fan),
 * the DB returns no row and the chip falls back to $0 — the next chat-list
 * tick will enrich the fan and the WS pump will populate spend on first
 * purchase/tip event.
 *
 * Cap is 200 ids per call (backend enforces); we chunk at 200 to match.
 */

import { useMemo } from "react";
import { useQueries } from "@tanstack/react-query";

import { relay, type OFChatItem } from "@/lib/relay";

export interface FanSpend {
  /** Lifetime total in cents. */
  spend_cents: number;
  /** ISO timestamp when the subscription ends, or null. */
  expired_at: string | null;
  /** Subscription status string: "active", "expired", etc. */
  subscription_status: string | null;
}

interface SpendBatchResp {
  spend: Record<string, {
    spend_cents: number;
    last_purchase_at: string | null;
    subscription_status: string | null;
    expired_at: string | null;
  }>;
}

const CHUNK = 200;

function chunk<T>(arr: T[], size: number): T[][] {
  const out: T[][] = [];
  for (let i = 0; i < arr.length; i += size) out.push(arr.slice(i, i + size));
  return out;
}

export function useFanSpend(rows: OFChatItem[]): Map<string, FanSpend> {
  const groups = useMemo(() => {
    const byAcct = new Map<string, Set<number>>();
    for (const r of rows) {
      const aid = r.__accountId;
      if (!aid) continue;
      if (!byAcct.has(aid)) byAcct.set(aid, new Set());
      byAcct.get(aid)!.add(r.withUser.id);
    }
    const out: Array<{ accountId: string; ids: number[]; key: string }> = [];
    for (const [aid, idSet] of byAcct) {
      const sorted = Array.from(idSet).sort((a, b) => a - b);
      for (const c of chunk(sorted, CHUNK)) {
        out.push({ accountId: aid, ids: c, key: c.join(",") });
      }
    }
    return out;
  }, [rows]);

  // `select` runs once per query response; the projected Map is memoized
  // by react-query and reused across renders. The outer hook only walks
  // the (small) groups list to stitch them together — no full re-scan of
  // the spend payload on every parent render.
  const queries = useQueries({
    queries: groups.map((g) => ({
      queryKey: ["fan-spend", g.accountId, g.key] as const,
      enabled: g.ids.length > 0,
      // 24h: DB is kept fresh by the WS pump + transcoder, so we don't
      // need to re-fetch on tab focus or interval. Chip persists across
      // reloads via providers.tsx PERSIST_PREFIXES.
      staleTime: 24 * 60 * 60 * 1000,
      refetchOnWindowFocus: false,
      queryFn: () => {
        const qs = new URLSearchParams({ ids: g.ids.join(",") });
        return relay.get<SpendBatchResp>(
          `/admin/fans/${g.accountId}/spend-batch?${qs.toString()}`,
        );
      },
      select: (resp: SpendBatchResp): Map<string, FanSpend> => {
        const m = new Map<string, FanSpend>();
        for (const [fidStr, entry] of Object.entries(resp.spend ?? {})) {
          m.set(`${g.accountId}:${fidStr}`, {
            spend_cents: entry.spend_cents,
            expired_at: entry.expired_at,
            subscription_status: entry.subscription_status,
          });
        }
        return m;
      },
    })),
  });

  const updatedKey = queries.map((q) => q.dataUpdatedAt).join("|");
  return useMemo(() => {
    const out = new Map<string, FanSpend>();
    for (const q of queries) {
      const projected = q.data as Map<string, FanSpend> | undefined;
      if (!projected) continue;
      for (const [k, v] of projected) out.set(k, v);
    }
    return out;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [updatedKey]);
}
