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

import { useSweepStatus } from "@/hooks/useVaultSweep";
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

/** One item's describe result. `ok` false carries `status` — the reason, which
 *  the row is stamped with (`refused`, `blocked_drm`, `fetch_failed`, …) — plus
 *  `detail`, the sentence to show a human. `capped` is the one an operator can
 *  act on: it means today's AI budget is spent, nothing was attempted, and the
 *  item is untouched. The rest of the payload is the AI fields the caller
 *  merges into the row it already holds. */
export interface DescribeResult extends Record<string, unknown> {
  media_id: number;
  ok: boolean;
  status: "described" | "capped" | "refused" | "error" | string;
  detail?: string;
}

export async function describeMedia(
  accountId: string, mediaId: number,
): Promise<DescribeResult> {
  return relay.post<DescribeResult>(
    `/admin/vault-ai/describe`,
    { account_id: accountId, media_id: mediaId },
    { accountId },
  );
}

/** Which bake-off prompt a describe pass uses.
 *  v2 = rich structured schema (acts / clothing_state / beats / primary_folder)
 *       — what auto-foldering needs. ~0.04¢ an item.
 *  v1 = the original one-sentence prompt, ~4× cheaper, no structured fields. */
export type PromptVersion = "v1" | "v2";

/** The start body for a describe pass. Posted by `useVaultSweep`, which owns
 *  every sweep's start call — this is only the shape of what describe adds on
 *  top of `account_id`. */
export function describeAllBody(opts: {
  force?: boolean;
  /** Re-scan rows produced by a DIFFERENT prompt version (resumable). */
  restage?: boolean;
  promptVersion?: PromptVersion;
  model?: string;
}): Record<string, unknown> {
  return {
    force: !!opts.force,
    restage: !!opts.restage,
    prompt_version: opts.promptVersion ?? "v2",
    ...(opts.model ? { model: opts.model } : {}),
  };
}

/** Today's spend against this account's daily LLM cap, for the VISION provider
 *  (describe + flags both bill there). The cap is per-(account, provider), so
 *  this is the budget a describe sweep actually spends — a busy chat day does
 *  not close describe down, and vice versa.
 *
 *  `capped` is the money, not the rollup row's sticky `is_capped` flag: raising
 *  the cap mid-day makes calls succeed again while that flag stays set.
 *
 *  `blocked_reason` is the server's own sentence, already formatted, and is the
 *  ONLY thing the UI should show. Composing it here as well would give one
 *  sentence two authors in two languages. Empty exactly when nothing blocks. */
export interface VisionCapState {
  day: string;
  provider: string;
  cap_millicents: number;
  spent_millicents: number;
  remaining_millicents: number;
  capped: boolean;
  /** The cap was hit at some point today, even if it has since been raised. */
  flagged: boolean;
  blocked_reason: string;
}

/** What each sweep mode would actually process — real counts for the UI. */
export function useDescribePlan(accountId: string | null, promptVersion: PromptVersion) {
  return useQuery<{
    total: number;
    undescribed: number;
    restage: number;
    prompt_version: PromptVersion;
    /** Absent only against a relay that predates the cap gate. */
    cap?: VisionCapState;
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
  /** A `-solo` cut of the lane it is named after — same lane, minus everyone
   *  else. Every folder carries this, so nothing has to test for its presence;
   *  `solo_of` is the size of the lane it was cut from ("12 of 34"), and is
   *  null exactly when `solo` is false. */
  solo: boolean;
  solo_of: number | null;
}

export interface AiFolderPlan {
  account_id: string;
  keep: number;
  solo: boolean;
  shoots_found: number;
  folders: AiFolder[];
  summary: {
    folders: number;
    scripts: number;
    /** Lanes NOT counting their solo cuts. */
    lanes: number;
    solo_cuts: number;
    unique_media: number;
    memberships: number;
  };
  /** Only computed when `solo` was asked for. `is_solo` refuses to call an item
   *  solo without the evidence (`people_count` / `partner_visible`, both V2
   *  describe fields), so on a V1-described vault the solo folders come out
   *  empty — correctly, and unreadably. This is what says why. */
  solo_coverage: {
    described: number;
    known: number;
    unknown: number;
    solo: number;
    ready: boolean;
  } | null;
  /** Coverage of the cheap pussy/breasts flags pass. When `ready` is false the
   *  paid tiers silently fall back to `clothing_state`, which was measured
   *  wrong on ~1 in 3 of the stills the $50 tier is built from — so the folders
   *  come out quietly wrong rather than visibly broken. Run /flags-all first. */
  flags: { stills: number; flagged: number; missing: number; ready: boolean };
}

export interface VaultFlagsStatus {
  running: boolean;
  progress: {
    total: number;
    done: number;
    failed: number;
    capped: boolean;
    cost_millicents: number;
    error?: string;
  } | null;
  coverage: { stills: number; flagged: number; missing: number; ready: boolean };
}

/** START the cheap exposure pass over an account's stills AND clips (~3s/item,
 *  ~$0.001 per 100). Enrichment only — merges the flags into each row and
 *  rewrites nothing else. Required before the paid tease tiers can be trusted.
 *
 *  Returns as soon as the sweep is queued. A whole vault takes minutes, which no
 *  proxy will hold a socket open for — waiting on the response is what showed
 *  "Internal Server Error" while the sweep ran on regardless. Poll
 *  `useVaultFlagsStatus` for progress. */
export async function runVaultFlags(accountId: string) {
  return relay.post<{ status: string; candidates: number; already: boolean }>(
    `/admin/vault-ai/flags-all`, { account_id: accountId }, { accountId },
  );
}

/** Poll the sweep while it runs. `coverage` comes from the DB, so a relay
 *  restart mid-sweep shows where the vault really stands rather than zero.
 *
 *  `pollWhileIdle` preserves this caller's own semantics: the folders modal arms
 *  the watch on the click and needs the poll running BEFORE the server admits
 *  the sweep exists, because it stops watching on the first landed answer that
 *  says `running: false` (see VaultAiFoldersModal's `since`). Self-arming would
 *  make that first answer arrive only after the sweep had already started. */
export function useVaultFlagsStatus(accountId: string | null, enabled: boolean) {
  return useSweepStatus<VaultFlagsStatus>("flags-all", accountId, {
    enabled,
    pollWhileIdle: enabled,
  });
}

/** What a folder plan is cut from: how many shoots stay whole, and whether the
 *  lanes also get their solo cut. Named rather than positional — these are two
 *  booleans and an int in a row, and at a call site `(id, 2, true, false)` is
 *  a bug waiting to be written. */
export interface FolderPlanOptions {
  keep?: number;
  /** Add a `-solo` cut of each lane, keeping only the items nobody else is in. */
  solo?: boolean;
}

/** Preview the folders the pipeline would create. Read-only — creates nothing.
 *
 *  The options are part of the query key because they are part of what the plan
 *  IS, and the SAME options have to reach `applyAiFolders` (see there). */
export function useAiFolderPlan(
  accountId: string | null,
  { keep = 2, solo = false }: FolderPlanOptions = {},
  enabled = false,
) {
  return useQuery<AiFolderPlan>({
    queryKey: ["vault-ai-folder-plan", accountId, keep, solo],
    enabled: !!accountId && enabled,
    queryFn: () =>
      relay.get(
        `/admin/vault-ai/folder-plan?account_id=${encodeURIComponent(accountId!)}&keep=${keep}` +
          `&solo=${solo ? "true" : "false"}`,
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
 *  than something `confirm` implies. Nothing is ever sent either way.
 *
 *  The plan options MUST match what the preview was showing. The server
 *  re-derives the plan and retires generated folders the current plan no longer
 *  makes, so applying without `solo` after previewing with it would create the
 *  lanes and delete every `-solo` folder in the same call. */
export async function applyAiFolders(
  accountId: string,
  { keep = 2, solo = false, mirrorToOf = false }:
    FolderPlanOptions & { mirrorToOf?: boolean } = {},
): Promise<ApplyAiFoldersResult> {
  return relay.post(
    `/admin/vault-ai/folder-plan/apply`,
    { account_id: accountId, keep, confirm: true, mirror_to_of: mirrorToOf, solo },
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
  /** Poster-frame count for a video (0 for a photo). Drives the hover-scrub. */
  frame_count: number;
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

/** One item awaiting the operator's eye. Pre-set to what the model believes;
 *  the operator taps only what is wrong. */
export interface FlagsReviewItem {
  media_id: number;
  kind: string;
  /** Why the system nominated this for a human look; empty = it did not. */
  iffy: string[];
  iffy_why: string[];
  lanes: string[];
  regions: Record<string, string>;
  over: Record<string, string>;
  description: string;
  clothing_state: string;
  explicitness: string;
  codes: string[];
  /** Regions already corrected by hand and frozen against re-runs. */
  locked: string[];
  graded: { v?: number; corrected?: string[]; note?: string };
}

export function useFlagsReview(
  accountId: string | null,
  enabled = false,
  onlyIffy = false,
  limit = 200,
) {
  return useQuery<{
    prompt_version: number;
    total: number;
    graded: number;
    iffy: number;
    only_iffy: boolean;
    items: FlagsReviewItem[];
  }>({
    queryKey: ["vault-flags-review", accountId, limit, onlyIffy],
    enabled: !!accountId && enabled,
    queryFn: () =>
      relay.get(
        `/admin/vault-ai/flags-review?account_id=${encodeURIComponent(accountId!)}` +
          `&limit=${limit}&only_iffy=${onlyIffy}`,
      ),
    staleTime: 15_000,
  });
}

export function useFlagsAccuracy(accountId: string | null, enabled = false) {
  return useQuery<{
    prompt_version: number;
    graded_items: number;
    graded_other_versions: number;
    answers: number;
    accuracy: number | null;
    per_region: Record<string, { ok: number; n: number }>;
  }>({
    queryKey: ["vault-flags-accuracy", accountId],
    enabled: !!accountId && enabled,
    queryFn: () =>
      relay.get(`/admin/vault-ai/flags-accuracy?account_id=${encodeURIComponent(accountId!)}`),
    staleTime: 15_000,
  });
}

/** Record one verdict. Corrections are locked permanently; everything left
 *  alone is recorded as CONFIRMED — which is not a no-op, it is the evidence
 *  the accuracy number runs on. Without it, "looked and agreed" and "never
 *  opened" are the same record. */
export async function gradeFlags(
  accountId: string,
  mediaId: number,
  corrections: Record<string, string>,
  note = "",
): Promise<{ ok: boolean; corrected: string[]; confirmed: string[] }> {
  return relay.post(
    `/admin/vault-ai/flags-review/grade`,
    { account_id: accountId, media_id: mediaId, corrections, note },
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

/** Relay-served cached thumb (permanent, signature-free) for a mirrored item.
 *  300x300 CENTRE-CROP — fine for a grid tile, wrong for judging exposure. */
export function mirrorThumbSrc(accountId: string, mediaId: number): string {
  return `/admin/vault-ai/thumb?account_id=${encodeURIComponent(accountId)}&media_id=${mediaId}`;
}

/** The FULL-FRAME image (aspect-preserving preview), permanent + signature-free.
 *  Use this anywhere an operator judges what is actually on show — the square
 *  thumb crops the top and bottom off a 3:4 portrait, hiding an edge-of-frame
 *  waistband or genitalia (the exact crop that misled the vision model). */
export function mirrorFullSrc(accountId: string, mediaId: number): string {
  return `/admin/vault-ai/image?account_id=${encodeURIComponent(accountId)}&media_id=${mediaId}`;
}

/** One of a video's poster frames (OF's own pre-extracted stills, media-id keyed
 *  and permanent — the same frames the chat VaultPicker slideshows). Used to
 *  hover-scrub a clip in the review UI: a video cannot be judged from a single
 *  still, so the operator drags across frame 0 → N-1 to see the whole thing. */
export function mirrorPosterSrc(accountId: string, mediaId: number, i: number): string {
  return `/admin/vault-ai/poster?account_id=${encodeURIComponent(accountId)}&media_id=${mediaId}&i=${i}`;
}
