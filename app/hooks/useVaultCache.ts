"use client";

/**
 * useVaultCache — the local whole-vault mirror ("Collect all").
 *
 *   summary  → GET  /admin/vault-ai/cache/summary   {count, running, last_run}
 *   collect  → POST /admin/vault-ai/collect          starts a sweep
 *   status   → GET  /admin/vault-ai/collect/status   progress (polled while running)
 *   items    → GET  /admin/vault-ai/items            read the mirror (instant local search)
 *
 * The mirror read returns the SAME {list, hasMore} shape as /api/of/v2/vault/media
 * (each item is the raw OF media dict + an `_ai` overlay), so the grid + <VaultTile/>
 * consume it unchanged — search is a local LIKE-scan, no OF round-trip.
 */

import { useInfiniteQuery, useQuery } from "@tanstack/react-query";

import { relay, type VaultMedia } from "@/lib/relay";

// Lazy-load in small chunks on scroll (not all at once — 244 images loading
// simultaneously is what made the grid slow). Infinite-scroll fetches the next
// page as the sentinel enters view.
const PAGE = 40;

export interface InternalFolder {
  id: number;
  name: string;
  of_list_id: number | null;
  count: number;
}

export function useInternalFolders(accountId: string | null) {
  return useQuery<{ folders: InternalFolder[] }>({
    queryKey: ["vault-internal-folders", accountId],
    enabled: !!accountId,
    queryFn: () =>
      relay.get(`/admin/vault-ai/folders?account_id=${encodeURIComponent(accountId!)}`),
    staleTime: 15_000,
  });
}

export async function createInternalFolder(accountId: string, name: string) {
  return relay.post(`/admin/vault-ai/folders`, { account_id: accountId, name }, { accountId });
}

export async function addToFolder(accountId: string, folderId: number, mediaIds: number[]) {
  return relay.post(
    `/admin/vault-ai/folders/${folderId}/add`,
    { account_id: accountId, media_ids: mediaIds },
    { accountId },
  );
}

/** Create a REAL OF vault folder (+ optionally add media). */
export async function createOfFolder(accountId: string, name: string, mediaIds: number[] = []) {
  return relay.post(
    `/admin/vault-ai/of-folders`,
    { account_id: accountId, name, media_ids: mediaIds },
    { accountId },
  );
}

/** Add media to a REAL OF folder (by OF list id). */
export async function addToOfFolder(accountId: string, listId: number, mediaIds: number[]) {
  return relay.post(
    `/admin/vault-ai/of-folders/${listId}/add`,
    { account_id: accountId, media_ids: mediaIds },
    { accountId },
  );
}

export async function renameOfFolder(accountId: string, listId: number, name: string) {
  return relay.post(
    `/admin/vault-ai/of-folders/${listId}/rename`,
    { account_id: accountId, name },
    { accountId },
  );
}

export async function deleteOfFolder(accountId: string, listId: number) {
  return relay.delete(
    `/admin/vault-ai/of-folders/${listId}?account_id=${encodeURIComponent(accountId)}`,
    { accountId },
  );
}

/** Set OF folder-list order: {sort,order} or a manual {customOrder:[ids]}. */
export async function sortOfFolders(
  accountId: string,
  opts: { sort?: string; order?: string; customOrder?: number[] },
) {
  return relay.post(
    `/admin/vault-ai/of-folders/sort`,
    { account_id: accountId, sort: opts.sort, order: opts.order, custom_order: opts.customOrder },
    { accountId },
  );
}

export interface OfFolderMirror {
  id: number;
  name: string;
  type: string;
  photosCount: number;
  videosCount: number;
  gifsCount: number;
  audiosCount: number;
  hasMedia: boolean;
  mediaCount: number;
  builtin: boolean;
}

/** OF folders derived from the local mirror's per-item listStates. Accurate
 *  where OF's live vault/lists lies (bogus hasMedia) or drops a folder
 *  entirely (e.g. a freshly-touched one). Non-empty folders only — the panel
 *  unions this with the live list to also show empty folders. */
export function useOfFoldersMirror(accountId: string | null, enabled = true) {
  return useQuery<{ list: OfFolderMirror[] }>({
    queryKey: ["vault-of-folders-mirror", accountId],
    enabled: enabled && !!accountId,
    queryFn: () =>
      relay.get(`/admin/vault-ai/of-folders?account_id=${encodeURIComponent(accountId!)}`),
    staleTime: 60_000,
  });
}

export async function describeMedia(accountId: string, mediaId: number) {
  return relay.post(`/admin/vault-ai/describe`, { account_id: accountId, media_id: mediaId }, {
    accountId,
  });
}

export async function startDescribeAll(accountId: string, force = false) {
  return relay.post(`/admin/vault-ai/describe-all`, { account_id: accountId, force }, { accountId });
}

/** Harvest OF's own vault search for `query` and fold it into our index, so
 *  local search becomes a superset of OF's. Cached server-side after first run. */
export async function searchOf(
  accountId: string,
  query: string,
): Promise<{ ids: number[]; count: number; source: string }> {
  return relay.post(`/admin/vault-ai/search-of`, { account_id: accountId, query }, { accountId });
}

/** Harvest OF's search across the whole "what sells" keyword set (background). */
export async function startHarvestKeywords(accountId: string) {
  return relay.post(`/admin/vault-ai/harvest-keywords`, { account_id: accountId }, { accountId });
}

export async function fetchHarvestStatus(
  accountId: string,
): Promise<{ running: boolean; progress: { total: number; done: number; matches: number } | null }> {
  return relay.get(`/admin/vault-ai/harvest-keywords/status?account_id=${encodeURIComponent(accountId)}`);
}

export async function fetchDescribeAllStatus(
  accountId: string,
): Promise<{ running: boolean; progress: { total: number; done: number; capped: boolean } | null }> {
  return relay.get(`/admin/vault-ai/describe-all/status?account_id=${encodeURIComponent(accountId)}`);
}

export async function reorderItems(
  accountId: string,
  folderId: number | null,
  order: { media_id: number; manual_order: number | null }[],
) {
  return relay.post(
    `/admin/vault-ai/reorder`,
    { account_id: accountId, folder_id: folderId, order },
    { accountId },
  );
}

export interface VaultCacheSummary {
  account_id: string;
  count: number;
  running: boolean;
  last_run: { id: number; status: string; total_seen: number; finished_at: string | null } | null;
}

export interface CollectRun {
  id: number;
  status: string;
  phase: string | null;
  total_seen: number;
  upserted: number;
  pages_done: number;
  error: string | null;
  started_at: string | null;
  finished_at: string | null;
  running: boolean;
}

/** Poll while a sweep is running so the button shows live progress. */
export function useVaultCacheSummary(accountId: string | null) {
  return useQuery<VaultCacheSummary>({
    queryKey: ["vault-cache-summary", accountId],
    enabled: !!accountId,
    queryFn: () =>
      relay.get<VaultCacheSummary>(
        `/admin/vault-ai/cache/summary?account_id=${encodeURIComponent(accountId!)}`,
      ),
    refetchInterval: (q) => (q.state.data?.running ? 1500 : false),
    staleTime: 10_000,
  });
}

export async function startCollect(accountId: string): Promise<{ run_id: number }> {
  return relay.post<{ run_id: number }>(
    `/admin/vault-ai/collect?account_id=${encodeURIComponent(accountId)}`,
    undefined,
    { accountId },
  );
}

export async function fetchCollectStatus(accountId: string): Promise<{ run: CollectRun | null }> {
  return relay.get<{ run: CollectRun | null }>(
    `/admin/vault-ai/collect/status?account_id=${encodeURIComponent(accountId)}`,
  );
}

export interface MirrorItemsOpts {
  accountId: string | null;
  type?: string;
  query?: string;
  sort?: "newest" | "oldest";
  internalFolderId?: number | null;
  ofFolderId?: number | null;
  enabled?: boolean;
}

interface MirrorResp {
  list: VaultMedia[];
  hasMore: boolean;
  source: string;
}

/** Paginated read of the LOCAL mirror. Same surface as useVaultMedia so the
 *  panel can swap between "Local (fast)" and "Live (OF)" transparently. */
export function useMirrorItems(opts: MirrorItemsOpts) {
  const {
    accountId, type = "all", query = "", sort = "newest",
    internalFolderId = null, ofFolderId = null, enabled = true,
  } = opts;
  const q = query.trim();
  const inf = useInfiniteQuery<MirrorResp>({
    queryKey: ["vault-mirror-items", accountId, type, sort, q, internalFolderId, ofFolderId],
    enabled: enabled && !!accountId,
    initialPageParam: 0,
    getNextPageParam: (last, all) => (last.hasMore ? all.length * PAGE : undefined),
    queryFn: async ({ pageParam }) => {
      const p = new URLSearchParams();
      p.set("account_id", accountId!);
      p.set("type", type);
      p.set("sort", sort);
      p.set("limit", String(PAGE));
      p.set("offset", String(pageParam ?? 0));
      if (q) p.set("query", q);
      if (internalFolderId != null) p.set("internal_folder_id", String(internalFolderId));
      if (ofFolderId != null) p.set("of_folder_id", String(ofFolderId));
      return relay.get<MirrorResp>(`/admin/vault-ai/items?${p.toString()}`);
    },
    staleTime: 30_000,
  });
  const items: VaultMedia[] = (inf.data?.pages ?? []).flatMap((p) => p.list ?? []);
  return {
    items,
    hasMore: !!inf.hasNextPage,
    isLoading: inf.isLoading,
    isFetching: inf.isFetching,
    loadMore: () => inf.fetchNextPage(),
    refetch: () => inf.refetch(),
  };
}
