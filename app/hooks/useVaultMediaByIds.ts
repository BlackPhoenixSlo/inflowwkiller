"use client";

/**
 * useVaultMediaByIds — resolve a handful of bare vault media ids back to full
 * VaultMedia objects (so the caller can render thumbnails).
 *
 * The Brain panel stores per-slot images as `time_images: {slot: media_id}` —
 * just the id, no thumbnail. The paginated vault list (useVaultMedia) only ever
 * yields full media for items on a loaded page, so a SAVED slot id whose item
 * sits deep in the vault never resolves and the UI falls back to a "#id" badge.
 *
 * OF exposes a real by-id read (`GET /api/of/v2/vault/media/{id}`, owner-gated)
 * but NO batch form, so we fan out one query per id via useQueries — at most
 * `MAX_IN_FLIGHT` of them at a time, see below. Each id is cached under its own
 * key (shared with any other caller resolving the same id) and a
 * deleted/unknown id 404s in isolation without sinking the others — that slot
 * just keeps its badge.
 *
 * Returns a plain `Record<number, VaultMedia>` of the ids that resolved.
 */

import { useMemo } from "react";
import { useQueries } from "@tanstack/react-query";

import { relay, type VaultMedia } from "@/lib/relay";

/**
 * React Query starts every query in a `useQueries` the moment it mounts — there
 * is no built-in concurrency limit. So a caller resolving a whole library
 * (PPVLibraryTab passes every stored media id) released one request per id at
 * once, and Next REWRITES `/api/of/*` server-side, so the browser's per-origin
 * socket limit is not what the relay sees: Node re-issues whatever reaches it.
 *
 * This is the cheap half of the brake and NOT the safety boundary — the server
 * caps this route too, which is what actually protects the process, because any
 * other tab, operator or script bypasses everything in this file. What the
 * client cap buys is that the requests never leave the tab in the first place:
 * no inbound socket, no auth middleware, no DB round-trip per queued id.
 */
const MAX_IN_FLIGHT = 6;
let inFlight = 0;
const waiting: Array<() => void> = [];

function release(): void {
  const next = waiting.shift();
  // HAND the slot over rather than decrementing — if we dropped the count and
  // let the woken caller re-increment, a caller arriving in between would see a
  // free slot that is already spoken for and the cap would drift upward.
  if (next) next();
  else inFlight -= 1;
}

async function withSlot<T>(run: () => Promise<T>): Promise<T> {
  if (inFlight >= MAX_IN_FLIGHT) {
    await new Promise<void>((resolve) => waiting.push(resolve));
  } else {
    inFlight += 1;
  }
  try {
    return await run();
  } finally {
    release();
  }
}

export function useVaultMediaByIds(
  accountId: string | null,
  ids: number[],
): Record<number, VaultMedia> {
  // De-dupe + drop falsy ids so a slot map with repeated/blank entries doesn't
  // spawn duplicate or wasted queries. Memoised off a stable signature so a
  // fresh array identity from the caller doesn't churn useQueries every render.
  const uniqueIds = useMemo(
    () => Array.from(new Set(ids.filter((n) => typeof n === "number" && n > 0))),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [accountId, ids.join(",")],
  );

  const queries = useQueries({
    queries: uniqueIds.map((id) => ({
      queryKey: ["vault-media-by-id", accountId, id] as const,
      enabled: !!accountId,
      queryFn: () =>
        withSlot(() =>
          relay.get<VaultMedia>(`/api/of/v2/vault/media/${id}`, { accountId: accountId ?? undefined }),
        ),
      // Vault items are owner-managed and rarely change; mirror useVaultMedia's
      // long staleTime so a resolved thumb sticks for the session.
      staleTime: 3 * 24 * 60 * 60_000,
      gcTime: 3 * 24 * 60 * 60_000,
      refetchOnMount: false,
      refetchOnWindowFocus: false,
      refetchOnReconnect: false,
      // A deleted slot image 404s — don't burn retries on a media that's gone.
      retry: false,
    })),
  });

  // useQueries hands back a fresh array each render; key the memo off the
  // per-query dataUpdatedAt so the map only rebuilds when real data lands.
  const updatedKey = queries.map((q) => q.dataUpdatedAt).join("|");
  return useMemo(() => {
    const out: Record<number, VaultMedia> = {};
    queries.forEach((q, i) => {
      const id = uniqueIds[i];
      const m = q.data;
      if (id && m && typeof m.id === "number") out[id] = m;
    });
    return out;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [updatedKey, uniqueIds]);
}
