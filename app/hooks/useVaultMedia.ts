"use client";

/**
 * useVaultMedia — paginated reader for the model's vault.
 *
 * Keys on (accountId, type, listId) so switching scope or filter swaps
 * cache cleanly. Pages are appended via loadMore so the user can scroll-
 * fetch in the picker. Returns merged list + hasMore + isLoading + error.
 *
 * No background polling — the vault is owner-managed, refresh is on demand.
 */

import { useEffect, useRef } from "react";
import { keepPreviousData, useInfiniteQuery, useQuery, useQueryClient } from "@tanstack/react-query";

import { relay, type RelayContext, type VaultList, type VaultListsResp, type VaultMedia, type VaultMediaResp } from "@/lib/relay";
import { perfDelivered, perfError, perfLog, perfOpId } from "@/lib/perfLog";
import { useUser } from "@/contexts/UserContext";
import { useChatter } from "@/contexts/ChatterContext";

const PAGE = 24;

export interface ChatterFolderGrant {
  folder_id: number;
  folder_name: string | null;
}

export interface ChatterFolderAccessResp {
  account_id: string;
  restricted: boolean;
  folders: ChatterFolderGrant[];
}

/**
 * Chatter Access (folders): the authoritative folder menu for a RESTRICTED
 * chatter session (DA-3). Only runs for a chatter-only principal — owners
 * (User cookie present) are never restricted, so the query is disabled and
 * the result reads `restricted: false` (full vault). The folder names come
 * from the backend's denormalised column, not from paginated vault/lists,
 * so the menu is complete even when a folder sits past the lists page.
 */
export function useChatterFolderAccess(accountId: string | null, enabled = true) {
  const { user } = useUser();
  const { chatter } = useChatter();
  const isChatterOnly = !user && !!chatter;
  return useQuery<ChatterFolderAccessResp>({
    queryKey: ["chatter", "folder-access", accountId],
    enabled: enabled && isChatterOnly && !!accountId,
    queryFn: () =>
      relay.get<ChatterFolderAccessResp>(
        `/chatter/me/folder-access?account_id=${encodeURIComponent(accountId!)}`,
      ),
    // Short staleTime so an owner's just-saved restriction takes effect on
    // the chatter's next picker open without a full reload. The backend
    // soft-denies regardless (no leak); this only governs the cosmetic menu.
    staleTime: 30_000,
    refetchOnWindowFocus: false,
  });
}

/** Fetch EVERY vault-folder page. OF pages `/vault/lists` (relay caps limit
 *  at 50/page) and built-in pseudo-lists count against the page too, so a
 *  creator with many script folders overflows page 1 — the old single-page
 *  fetch silently dropped everything past it. Walks offsets until hasMore
 *  is false, with a hard page cap so a stuck hasMore can't loop forever.
 *
 *  Every reader/warmer of the ["vault-lists", aid] cache MUST go through
 *  this — a single-page queryFn under the same key would repoison the
 *  cache with a truncated folder list for its whole staleTime. */
export async function fetchAllVaultLists(ctx: RelayContext): Promise<VaultListsResp> {
  const LISTS_PAGE = 50;
  const merged: VaultList[] = [];
  let offset = 0;
  for (let page = 0; page < 20; page++) {
    const r = await relay.get<VaultListsResp>(
      `/api/of/v2/vault/lists?view=main&limit=${LISTS_PAGE}&offset=${offset}`,
      ctx,
    );
    const rows = r.list ?? [];
    merged.push(...rows);
    if (!r.hasMore || rows.length === 0) break;
    offset += LISTS_PAGE;
  }
  return { list: merged, hasMore: false };
}

/** Vault folders / lists. The model can put items into custom folders;
 *  this drives the picker's folder filter dropdown. OF requires `view=main`. */
export function useVaultLists(accountId: string | null, enabled = true) {
  return useQuery<VaultListsResp>({
    queryKey: ["vault-lists", accountId],
    enabled: enabled && !!accountId,
    queryFn: async () => {
      const opId = perfOpId("vault.lists");
      perfLog(opId, "vault.lists", "requested", { accountId });
      try {
        const r = await fetchAllVaultLists({ accountId: accountId ?? undefined });
        perfDelivered(opId, "vault.lists", { count: (r.list ?? []).length });
        return r;
      } catch (err) {
        perfError(opId, "vault.lists", { message: (err as Error)?.message });
        throw err;
      }
    },
    staleTime: 5 * 60 * 1000,
    refetchOnWindowFocus: false,
    select: (d) => ({
      // Only show custom folders — the built-in pseudo-lists like Posts/Stories
      // are useless for picking media to send. media_stickers (Uploads) is
      // built-in too but we leave it; it's where freshly-uploaded items land.
      list: (d.list ?? []).filter((l: VaultList) => l.type === "custom" && l.hasMedia),
    }),
  });
}

export interface UseVaultMediaOpts {
  accountId: string | null;
  type?: "all" | "photo" | "video" | "gif" | "audio";
  listId?: number | null;
  enabled?: boolean;
  /** OF honors server-side ordering — "oldest" maps to `sort=asc`. Kept out
   *  of client-side reversal so pagination stays correct (older pages append
   *  at the bottom, not the top). */
  sort?: "newest" | "oldest";
  /** OF-native vault search (wire param `query`). Empty = no filter. */
  query?: string;
}

export function useVaultMedia(opts: UseVaultMediaOpts) {
  const {
    accountId, type = "all", listId = null, enabled = true,
    sort = "newest", query = "",
  } = opts;
  const queryTrimmed = query.trim();
  const qc = useQueryClient();
  // Flipped to `true` by `refresh()` so the very next queryFn run appends
  // `&refresh=1` and bypasses the shared server-side vault cache. Cleared
  // inside the queryFn after the param is consumed, so a normal scroll-
  // triggered loadMore doesn't accidentally inherit the bypass.
  const bypassServerCacheRef = useRef(false);

  const q = useInfiniteQuery<VaultMediaResp>({
    queryKey: ["vault-media", accountId, type, listId, sort, queryTrimmed],
    enabled: enabled && !!accountId,
    initialPageParam: 0,
    getNextPageParam: (last, all) =>
      last.hasMore ? all.length * PAGE : undefined,
    queryFn: async ({ pageParam, signal }) => {
      const params = new URLSearchParams();
      params.set("limit", String(PAGE));
      params.set("offset", String(pageParam ?? 0));
      params.set("type", type);
      // OF orders server-side; asc = oldest-first. Sorting here (not a
      // client reverse) keeps infinite-scroll appending in the right place.
      params.set("sort", sort === "oldest" ? "asc" : "desc");
      if (listId != null) params.set("list_id", String(listId));
      if (queryTrimmed) params.set("query", queryTrimmed);
      if (bypassServerCacheRef.current) {
        params.set("refresh", "1");
        bypassServerCacheRef.current = false;
      }
      // Per-fetch perf op. `vault.media` covers the initial fetch (the one
      // the user feels as "vault open"), filter switches (a fresh fetch
      // under a new queryKey), and infinite-scroll page loads. We tag the
      // op with offset so the log distinguishes them at a glance.
      const offset = (pageParam as number) ?? 0;
      const opId = perfOpId("vault.media");
      perfLog(opId, "vault.media", "requested", {
        accountId, type, listId, offset, phase: offset === 0 ? "initial" : "page",
      });
      // Forward the abort signal so switching folder/type cancels the
      // previous page fetch instead of letting it land into a stale key.
      try {
        const r = await relay.get<VaultMediaResp>(
          `/api/of/v2/vault/media?${params.toString()}`,
          { accountId: accountId ?? undefined },
          signal,
        );
        perfDelivered(opId, "vault.media", {
          count: (r.list ?? []).length, hasMore: !!r.hasMore, offset,
        });
        return r;
      } catch (err) {
        perfError(opId, "vault.media", {
          message: (err as Error)?.message, offset, aborted: signal?.aborted,
        });
        throw err;
      }
    },
    staleTime: 3 * 24 * 60 * 60_000,
    placeholderData: keepPreviousData,
  });

  // Once page 1 lands, eagerly pull page 2 in the background so the first
  // scroll past the fold is already cached. Guarded on `pages.length === 1`
  // so we only fire the chained prefetch once per cache miss — not on
  // every render and not after the user has already scrolled further.
  useEffect(() => {
    if (q.data?.pages.length === 1 && q.hasNextPage && !q.isFetchingNextPage) {
      q.fetchNextPage();
    }
  }, [q.data?.pages.length, q.hasNextPage, q.isFetchingNextPage, q]);

  const items: VaultMedia[] = (q.data?.pages ?? []).flatMap((p) => p.list ?? []);
  return {
    items,
    hasMore: !!q.hasNextPage,
    isLoading: q.isLoading,
    isFetching: q.isFetching,
    isFetchingNextPage: q.isFetchingNextPage,
    isPlaceholderData: q.isPlaceholderData,
    error: q.error,
    loadMore: () => q.fetchNextPage(),
    /** Hard refresh — drops every cached page for this account's vault
     *  (any type/listId combo, the wall-media cache, and the lists cache)
     *  so the next render starts from a blank slate. The plain `q.refetch()`
     *  only refires the queries this hook owns; the picker switches the
     *  `type` chip a lot, so we want every adjacent cache nuked too. */
    refresh: async () => {
      // Await the server-side invalidate before refetching so vault-lists
      // (which uses a separate hook with no `?refresh=1` bypass flag)
      // doesn't race against a still-cached row. The vault-media call
      // sets `bypassServerCacheRef` as belt-and-braces. The invalidate
      // POST is a single DELETE — adds ~30-80ms to a user-initiated
      // refresh, which is invisible next to the OF roundtrip that follows.
      bypassServerCacheRef.current = true;
      qc.removeQueries({ queryKey: ["vault-media", accountId] });
      qc.removeQueries({ queryKey: ["vault-lists", accountId] });
      qc.removeQueries({ queryKey: ["wall-media", accountId] });
      if (accountId) {
        try {
          await relay.post("/admin/vault/cache/invalidate", undefined, { accountId });
        } catch { /* invalidation is best-effort — fall through to refetch */ }
      }
      return q.refetch();
    },
  };
}
