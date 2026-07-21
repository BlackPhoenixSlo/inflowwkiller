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

/** Which bake-off prompt a describe pass uses.
 *  v2 = rich structured schema (acts / clothing_state / beats / primary_folder)
 *       — what auto-foldering needs. ~0.04¢ an item.
 *  v1 = the original one-sentence prompt, ~4× cheaper, no structured fields. */
export type PromptVersion = "v1" | "v2";

export interface DescribeAllOpts {
  force?: boolean;
  /** Re-scan rows produced by a DIFFERENT prompt version (resumable). */
  restage?: boolean;
  promptVersion?: PromptVersion;
  model?: string;
}

export async function startDescribeAll(
  accountId: string,
  optsOrForce: DescribeAllOpts | boolean = false,
) {
  const o: DescribeAllOpts =
    typeof optsOrForce === "boolean" ? { force: optsOrForce } : optsOrForce;
  return relay.post(
    `/admin/vault-ai/describe-all`,
    {
      account_id: accountId,
      force: !!o.force,
      restage: !!o.restage,
      prompt_version: o.promptVersion ?? "v2",
      ...(o.model ? { model: o.model } : {}),
    },
    { accountId },
  );
}

/** What each sweep mode would actually process — real counts for the UI. */
export function useDescribePlan(accountId: string | null, promptVersion: PromptVersion) {
  return useQuery<{
    total: number;
    undescribed: number;
    restage: number;
    prompt_version: PromptVersion;
  }>({
    queryKey: ["vault-describe-plan", accountId, promptVersion],
    enabled: !!accountId,
    queryFn: () =>
      relay.get(
        `/admin/vault-ai/describe-all/plan?account_id=${encodeURIComponent(accountId!)}` +
          `&prompt_version=${promptVersion}`,
      ),
    staleTime: 30_000,
  });
}

// ── AI folder plan ────────────────────────────────────────────────
//
// The vault's "Build AI folders" button. Two steps, deliberately: the plan is
// a free read-only preview, and NOTHING is created until the operator confirms.
// The apply call re-derives the plan server-side rather than posting the
// previewed ids back, so a preview left open for ten minutes can't write a
// stale grouping.

export interface AiFolderItem {
  media_id: number;
  kind: string;
  manual_order: number;
  score: number | null;
  tier: string;
  closes: boolean;
  clothing_state: string;
}

export interface AiFolder {
  name: string;
  source: "script" | "lane";
  purpose: string;
  size: number;
  kinds: Record<string, number>;
  tiers: Record<string, number>;
  closes_on_own: number;
  items: AiFolderItem[];
  outfit?: string;
  mixed?: boolean;
  lane?: string;
}

export interface AiFolderPlan {
  account_id: string;
  keep: number;
  shoots_found: number;
  folders: AiFolder[];
  summary: {
    folders: number;
    scripts: number;
    lanes: number;
    unique_media: number;
    memberships: number;
  };
  /** Coverage of the cheap pussy/breasts flags pass. When `ready` is false the
   *  paid tiers silently fall back to `clothing_state`, which was measured
   *  wrong on ~1 in 3 of the stills the $50 tier is built from — so the folders
   *  come out quietly wrong rather than visibly broken. Run /flags-all first. */
  flags: { stills: number; flagged: number; missing: number; ready: boolean };
}

/** Run the cheap two-boolean pass over an account's stills (~3s/item, ~$0.001
 *  per 100). Enrichment only — merges the flags into each row and rewrites
 *  nothing else. Required before the paid tease tiers can be trusted. */
export async function runVaultFlags(accountId: string) {
  return relay.post(`/admin/vault-ai/flags-all`, { account_id: accountId }, { accountId });
}

/** Preview the folders the pipeline would create. Read-only — creates nothing. */
export function useAiFolderPlan(accountId: string | null, keep = 2, enabled = false) {
  return useQuery<AiFolderPlan>({
    queryKey: ["vault-ai-folder-plan", accountId, keep],
    enabled: !!accountId && enabled,
    queryFn: () =>
      relay.get(
        `/admin/vault-ai/folder-plan?account_id=${encodeURIComponent(accountId!)}&keep=${keep}`,
      ),
    staleTime: 30_000,
  });
}

export interface ApplyAiFoldersResult {
  folders: number;
  new: number;
  reused: number;
  items: number;
  of_mirrored?: number;
  of_failed?: number;
  created: {
    folder_id: number;
    name: string;
    items: number;
    reused: boolean;
    of_list_id?: number;
    of_error?: string;
  }[];
}

/** Create the previewed folders. `confirm` is required server-side, so there is
 *  no single-click path from browsing to writing. Re-running refreshes the same
 *  `AI-` folders instead of duplicating them, and never touches a folder the
 *  operator made by hand.
 *
 *  `mirrorToOf` additionally creates them as REAL OF vault lists — the only
 *  write to her OnlyFans account in this flow, hence a separate flag rather
 *  than something `confirm` implies. Nothing is ever sent either way. */
export async function applyAiFolders(
  accountId: string,
  keep = 2,
  mirrorToOf = false,
): Promise<ApplyAiFoldersResult> {
  return relay.post(
    `/admin/vault-ai/folder-plan/apply`,
    { account_id: accountId, keep, confirm: true, mirror_to_of: mirrorToOf },
    { accountId },
  );
}

/** One item where the describe pass and the flags pass contradict each other.
 *  Both readings are carried, plus the two literal field-writes that would
 *  settle it — which pass is wrong differs item by item, so the operator picks
 *  and nothing is decided for them. */
export interface VaultDispute {
  media_id: number;
  kind: string;
  codes: string[];
  reasons: string[];
  description: string;
  clothing_state: string;
  explicitness: string;
  underwear_visible: boolean | null;
  regions: Record<string, string>;
  over: Record<string, string>;
  /** Fields already corrected by hand and locked against re-runs. */
  resolved: string[];
  propose: { flags: Record<string, unknown>; describe: Record<string, unknown> };
}

export function useVaultDisputes(accountId: string | null, enabled = false) {
  return useQuery<{ checked: number; count: number; disputes: VaultDispute[] }>({
    queryKey: ["vault-disputes", accountId],
    enabled: !!accountId && enabled,
    queryFn: () =>
      relay.get(`/admin/vault-ai/disputes?account_id=${encodeURIComponent(accountId!)}`),
    staleTime: 15_000,
  });
}

/** Apply ONE operator correction. Written into the AI fields (so every reader
 *  sees it), recorded as an override, and LOCKED — a forced re-flag refreshes
 *  everything except this, because a correction a re-run reverts is worse than
 *  no correction at all. */
export async function resolveDispute(
  accountId: string,
  mediaId: number,
  values: Record<string, unknown>,
): Promise<{ ok: boolean; applied: Record<string, unknown>; still_disagrees: string[] }> {
  return relay.post(
    `/admin/vault-ai/disputes/resolve`,
    { account_id: accountId, media_id: mediaId, values },
    { accountId },
  );
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
    // Distinct from isFetching: a background refetch of page 1 (invalidate,
    // stale-revalidate) also flips isFetching, and gating "load more" on that
    // silently swallows the scroll trigger. Only a real next-page fetch blocks.
    isFetchingNextPage: inf.isFetchingNextPage,
    error: inf.error,
    loadMore: () => {
      if (inf.hasNextPage && !inf.isFetchingNextPage) inf.fetchNextPage();
    },
    refetch: () => inf.refetch(),
  };
}

// ── Duplicate detection ───────────────────────────────────────────
//
// Re-uploads are ~30% of a mature vault. `original` is the earliest-uploaded
// copy and is always KEPT; `dupes` are the removal candidates. Hiding calls
// OF's own "Remove from vault" (PUT /vault/media/hidden) — the copies leave
// her real vault but are not destroyed, so sent PPVs keep working. OF has no
// unhide, which is why the UI confirms first.

export interface DupeMember {
  media_id: number;
  kind: string;
  created_at: string | null;
  duration_seconds: number | null;
  send_count: number;
  dhash_dist?: number;
  ahash_dist?: number;
  exact?: boolean;
  band?: string;
}

export interface DupeCluster {
  original: DupeMember;
  dupes: DupeMember[];
  band: string;
  worst: number;
  all_exact: boolean;
  sent_dupes: number;
}

export interface DupeScan {
  account_id: string;
  threshold: number;
  scanned: number;
  unhashed: number;
  sets: number;
  removable: number;
  returned: number;
  clusters: DupeCluster[];
}

/** Scan for duplicate sets. Read-only — nothing is hidden by this call. */
export function useDuplicates(accountId: string | null, threshold = 2, enabled = false) {
  return useQuery<DupeScan>({
    queryKey: ["vault-duplicates", accountId, threshold],
    enabled: !!accountId && enabled,
    queryFn: () =>
      relay.get(
        `/admin/vault-ai/duplicates?account_id=${encodeURIComponent(accountId!)}` +
          `&threshold=${threshold}`,
      ),
    staleTime: 60_000,
  });
}

export interface HideDupesResult {
  hidden: number;
  hidden_ids: number[];
  refused: Record<string, number[]>;
  of_error?: string;
  not_hidden?: number[];
}

// The relay caps one request at 500 (blast-radius guard) and a mature vault
// yields ~700 copies, so the selection is sent in batches. Sequential, not
// parallel: these are real writes against her OF account.
const HIDE_BATCH = 250;

/** Remove confirmed copies from the REAL OF vault. The relay re-clusters and
 *  refuses anything that is an original / not a duplicate / already sent.
 *  Batches transparently and aggregates; `onProgress` reports ids completed.
 *  Stops early if a batch reports a partial OF failure so the caller can
 *  re-offer the remainder rather than pushing on blindly. */
export async function hideDuplicates(
  accountId: string,
  mediaIds: number[],
  opts: {
    threshold?: number;
    allowSent?: boolean;
    onProgress?: (done: number, total: number) => void;
  } = {},
): Promise<HideDupesResult> {
  const agg: HideDupesResult = { hidden: 0, hidden_ids: [], refused: {} };
  for (let i = 0; i < mediaIds.length; i += HIDE_BATCH) {
    const batch = mediaIds.slice(i, i + HIDE_BATCH);
    const res: HideDupesResult = await relay.post(
      `/admin/vault-ai/duplicates/hide`,
      {
        account_id: accountId,
        media_ids: batch,
        threshold: opts.threshold ?? 2,
        allow_sent: !!opts.allowSent,
      },
      { accountId },
    );
    agg.hidden += res.hidden ?? 0;
    agg.hidden_ids.push(...(res.hidden_ids ?? []));
    for (const [why, ids] of Object.entries(res.refused ?? {})) {
      agg.refused[why] = [...(agg.refused[why] ?? []), ...ids];
    }
    opts.onProgress?.(Math.min(i + batch.length, mediaIds.length), mediaIds.length);
    if (res.of_error) {
      agg.of_error = res.of_error;
      agg.not_hidden = [...(res.not_hidden ?? []), ...mediaIds.slice(i + batch.length)];
      break;
    }
  }
  return agg;
}

/** Relay-served cached thumb (permanent, signature-free) for a mirrored item. */
export function mirrorThumbSrc(accountId: string, mediaId: number): string {
  return `/admin/vault-ai/thumb?account_id=${encodeURIComponent(accountId)}&media_id=${mediaId}`;
}
