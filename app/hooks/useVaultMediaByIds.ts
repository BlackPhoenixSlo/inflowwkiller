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
 * but NO batch form, so we fan out one query per id via useQueries. Each id is
 * cached under its own key (shared with any other caller resolving the same id)
 * and a deleted/unknown id 404s in isolation without sinking the others — that
 * slot just keeps its badge.
 *
 * Returns a plain `Record<number, VaultMedia>` of the ids that resolved.
 */

import { useMemo } from "react";
import { useQueries } from "@tanstack/react-query";

import { relay, type VaultMedia } from "@/lib/relay";

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
        relay.get<VaultMedia>(`/api/of/v2/vault/media/${id}`, { accountId: accountId ?? undefined }),
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
