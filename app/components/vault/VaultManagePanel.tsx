"use client";

/**
 * VaultManagePanel — the Vault manager.
 *
 * OF-style two-panel layout. Folders ARE OnlyFans folders (unified — no separate
 * internal folders): the rail lists OF's real vault folders, served from the
 * local mirror (instant, exact counts) and reorderable per-folder. "New folder"
 * and "Add to folder" write to OF for real. Search is AI vision tags ∪ OF's own
 * search. Collect / Describe-all / Harvest run in the background.
 */

import { type DragEvent as ReactDragEvent, useEffect, useMemo, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { AccountChips } from "@/components/automations/ReadyMadePanel";
import {
  isDrmOnlyVideo,
  progressiveVideoSrc,
  VaultVideoPreview,
} from "@/components/chat/VaultPicker";
import VaultAiFoldersModal from "@/components/vault/VaultAiFoldersModal";
import VaultDisputesModal from "@/components/vault/VaultDisputesModal";
import VaultFlagsReviewModal from "@/components/vault/VaultFlagsReviewModal";
import VaultDuplicatesModal from "@/components/vault/VaultDuplicatesModal";
import VaultTile from "@/components/vault/VaultTile";
import { useActiveAccounts } from "@/hooks/useAccounts";
import { useVaultLists, useVaultMedia } from "@/hooks/useVaultMedia";
import {
  addToOfFolder,
  createOfFolder,
  deleteOfFolder,
  describeMedia,
  useOfFoldersMirror,
  fetchCollectStatus,
  fetchDescribeAllStatus,
  fetchHarvestStatus,
  renameOfFolder,
  reorderItems,
  searchOf,
  sortOfFolders,
  startCollect,
  startDescribeAll,
  useDescribePlan,
  startHarvestKeywords,
  useMirrorItems,
  useVaultCacheSummary,
  mirrorFullSrc,
} from "@/hooks/useVaultCache";
import { proxyImage, relay, type VaultList, type VaultMedia } from "@/lib/relay";
import ImageLightbox from "@/components/vault/ImageLightbox";

type MediaType = "all" | "photo" | "video" | "gif" | "audio";
type Tile = VaultMedia & { _ai?: Record<string, unknown> };

// explicitness tiers the pricing bands key off (contract §1 bands_by_tier).
/** The three answers the flags pass gives per region. Must match
 *  `vault_ai_brief.VIS_STATES` — the server rejects anything else with a 400
 *  rather than storing a value no lane knows how to read. */
const VIS_OPTIONS = ["bare", "covered", "not_in_frame"];

const TIER_OPTIONS = ["safe", "suggestive", "explicit", "graphic", "unknown"];

/** PATCH /admin/vault-ai/items/{id} — override + lock the operator-edited fields
 *  (contract §6c). `fields` carries only the changed keys so only those lock. */
async function saveVaultItem(
  accountId: string,
  mediaId: number,
  fields: Record<string, unknown>,
): Promise<{ item: Record<string, unknown>; locked: string[] }> {
  return relay.patch(
    `/admin/vault-ai/items/${mediaId}`,
    { account_id: accountId, fields },
    { accountId },
  );
}

/** POST /admin/vault-ai/items/{id}/hide — OF's own "Remove from vault" (the same
 *  call the duplicates sweep uses). The media is HIDDEN on OF, not destroyed, so
 *  already-sent PPVs and live posts keep working — but OF exposes no unhide,
 *  which is why the UI confirms first. */
async function hideVaultItem(accountId: string, mediaId: number): Promise<void> {
  await relay.post(
    `/admin/vault-ai/items/${mediaId}/hide`,
    { account_id: accountId },
    { accountId },
  );
}

const TYPE_TABS: { key: MediaType; label: string }[] = [
  { key: "all", label: "All" },
  { key: "photo", label: "Photo" },
  { key: "gif", label: "GIF" },
  { key: "video", label: "Video" },
  { key: "audio", label: "Audio" },
];

function cx(...parts: (string | false | null | undefined)[]): string {
  return parts.filter(Boolean).join(" ");
}

function folderCounts(f: VaultList): string {
  const bits: string[] = [];
  if (f.photosCount) bits.push(`🖼 ${f.photosCount}`);
  if (f.videosCount) bits.push(`🎬 ${f.videosCount}`);
  if (f.gifsCount) bits.push(`GIF ${f.gifsCount}`);
  if (f.audiosCount) bits.push(`🎤 ${f.audiosCount}`);
  return bits.join(" · ") || "empty";
}

export default function VaultManagePanel() {
  const accounts = useActiveAccounts();
  const qc = useQueryClient();
  const [accountId, setAccountId] = useState<string | null>(null);
  const [type, setType] = useState<MediaType>("all");
  const [ofFolderId, setOfFolderId] = useState<number | null>(null); // OF folder (list id)
  const [sort, setSort] = useState<"newest" | "oldest">("newest");
  const [searchRaw, setSearchRaw] = useState("");
  const [query, setQuery] = useState("");
  const [preview, setPreview] = useState<Tile | null>(null);
  // Full-screen playback target — hands off to the picker's VaultVideoPreview.
  const [playMedia, setPlayMedia] = useState<VaultMedia | null>(null);
  const [selectMode, setSelectMode] = useState(false);
  const [showDupes, setShowDupes] = useState(false);
  const [showAiFolders, setShowAiFolders] = useState(false);
  const [showDisputes, setShowDisputes] = useState(false);
  const [showFlagsReview, setShowFlagsReview] = useState(false);
  const [needsReview, setNeedsReview] = useState(0);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [busy, setBusy] = useState<string>("");
  const [describeAll, setDescribeAll] = useState<string>("");
  const [harvest, setHarvest] = useState<string>("");
  const [promptVersion, setPromptVersion] = useState<"v1" | "v2">("v2");
  const drawerRef = useRef<HTMLElement | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const sentinelRef = useRef<HTMLDivElement | null>(null);
  const loadMoreRef = useRef<() => void>(() => {});
  const canLoadRef = useRef(false);

  useEffect(() => {
    if (!accountId && accounts.length > 0) setAccountId(accounts[0].id);
  }, [accounts, accountId]);
  useEffect(() => {
    const t = setTimeout(() => setQuery(searchRaw.trim()), 250);
    return () => clearTimeout(t);
  }, [searchRaw]);

  // Esc closes the full-screen player first, then the details drawer.
  useEffect(() => {
    if (!preview && !playMedia) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      if (playMedia) setPlayMedia(null);
      else setPreview(null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [preview, playMedia]);

  // Clicking anywhere outside the drawer closes it, same as the ✕. Tile clicks
  // still read as a SWAP rather than a close: this fires first, then the tile's
  // own onClick sets the new preview in the same React batch. Skipped while the
  // full-screen player is up, so clicking the player can't close the drawer
  // underneath it.
  useEffect(() => {
    if (!preview || selectMode || playMedia) return;
    // Phone only: the drawer is a full-screen sheet there, so there is no
    // "outside" to click — and a thumb brushing the edge would close it.
    if (!window.matchMedia("(min-width: 768px)").matches) return;
    const onDown = (e: MouseEvent) => {
      const el = drawerRef.current;
      if (el && !el.contains(e.target as Node)) setPreview(null);
    };
    document.addEventListener("pointerdown", onDown);
    return () => document.removeEventListener("pointerdown", onDown);
  }, [preview, selectMode, playMedia]);

  // On a search, harvest OF's own results in the BACKGROUND and merge them in.
  useEffect(() => {
    if (!accountId || !query) return;
    let cancelled = false;
    searchOf(accountId, query)
      .then(() => {
        if (!cancelled) qc.invalidateQueries({ queryKey: ["vault-mirror-items", accountId] });
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [query, accountId, qc]);

  const summary = useVaultCacheSummary(accountId);
  const plan = useDescribePlan(accountId, promptVersion);
  const cachedCount = summary.data?.count ?? 0;
  const isCollecting = !!summary.data?.running;
  const run = summary.data?.last_run;

  // includeEmpty: the manage rail shows ALL custom folders (even empty ones) so
  // you can add media to / rename / delete them — unlike the picker which hides
  // empty folders. Both share the same paginated fetch cache; only the filter differs.
  const lists = useVaultLists(accountId, true, true);
  // Mirror-derived OF folders (accurate counts; recovers folders OF's live list
  // drops, e.g. "nowfolder"). Only meaningful once collected.
  const ofFoldersMirror = useOfFoldersMirror(accountId, cachedCount > 0);
  // Union: start from the live custom folders (they include EMPTY folders the
  // mirror can't see), override counts from the mirror where it has them, then
  // append any mirror-only custom folder the live list omitted.
  const folders = useMemo<VaultList[]>(() => {
    const live = (lists.data?.list ?? []) as VaultList[];
    const mir = (ofFoldersMirror.data?.list ?? []).filter((f) => !f.builtin);
    if (mir.length === 0) return live;
    const liveIds = new Set(live.map((f) => f.id));
    const mirById = new Map(mir.map((f) => [f.id, f]));
    const merged: VaultList[] = live.map((f) => {
      const mm = mirById.get(f.id);
      return mm
        ? {
            ...f,
            hasMedia: true,
            photosCount: mm.photosCount,
            videosCount: mm.videosCount,
            gifsCount: mm.gifsCount,
            audiosCount: mm.audiosCount,
          }
        : f;
    });
    for (const mm of mir) {
      if (liveIds.has(mm.id)) continue;
      merged.push({
        id: mm.id,
        name: mm.name,
        type: "custom",
        hasMedia: true,
        photosCount: mm.photosCount,
        videosCount: mm.videosCount,
        gifsCount: mm.gifsCount,
        audiosCount: mm.audiosCount,
      });
    }
    return merged;
  }, [lists.data, ofFoldersMirror.data]);

  // Everything is served from the mirror once collected (instant, incl. OF
  // folders + reorder). Before the first collect, fall back to live OF all-media.
  // IMPORTANT: don't kick off the SLOW live-OF fetch until the (fast, local)
  // summary has resolved — otherwise every mount/account-switch treats the
  // brief "summary still loading" window as "no cache", fires a live OF round
  // trip ("Loading…"), and only later swaps to the instant mirror. Wait first.
  const summaryReady = !summary.isLoading;
  const useLocal = cachedCount > 0;
  const usingFolder = ofFolderId != null;
  const live = useVaultMedia({
    accountId, type, listId: null, sort, query, enabled: summaryReady && !useLocal,
  });
  const mirror = useMirrorItems({
    accountId, type, query, sort, ofFolderId, enabled: useLocal,
  });
  const src = useLocal ? mirror : live;
  // Show the spinner while we're still deciding (summary pending) OR the chosen
  // source is fetching — never flash an empty state during the swap.
  const showLoading = summary.isLoading || src.isLoading;
  const items = src.items as Tile[];
  const pageFetching = src.isFetchingNextPage;
  const pageError = !!src.error && !pageFetching;
  loadMoreRef.current = src.loadMore;
  // Gate on isFetchingNextPage, NOT isFetching: any background refetch of the
  // pages we already hold (invalidate after a describe/edit, stale revalidate)
  // flips isFetching, and gating on it wedges the trigger permanently.
  canLoadRef.current = src.hasMore && !pageFetching;

  useEffect(() => {
    const el = sentinelRef.current;
    if (!el) return;
    // Observe scroll INSIDE the media pane (scrollRef), not the page viewport —
    // the grid is its own scroll container now. `items.length` in the deps
    // re-attaches the observer once the sentinel mounts after the first load
    // (it doesn't exist while isLoading), otherwise nothing fires at the bottom.
    //
    // `pageFetching` is a dep too, so the observer is RE-ARMED every time a page
    // fetch settles. Without it, a user already parked at the bottom sees the
    // sentinel stay continuously intersecting: the callback fired once (or fired
    // while a fetch was in flight, or the fetch failed and items.length never
    // moved) and IO never fires again without a state change. Re-observing
    // re-delivers the current intersection, so loading resumes on its own.
    const obs = new IntersectionObserver(
      (entries) => {
        if (entries[0]?.isIntersecting && canLoadRef.current) loadMoreRef.current();
      },
      { root: scrollRef.current, rootMargin: "600px" },
    );
    obs.observe(el);
    return () => obs.disconnect();
  }, [accountId, ofFolderId, type, query, sort, items.length, pageFetching]);

  // Belt-and-braces: a plain scroll listener on the pane. The observer covers
  // the normal case, but it is one silent failure (root not yet mounted, a
  // dropped page) away from stranding the grid — and the user is then stuck
  // with no way to ask for more. This never wedges: it re-evaluates on every
  // scroll event.
  function onGridScroll() {
    const el = scrollRef.current;
    if (!el || !canLoadRef.current) return;
    if (el.scrollHeight - el.scrollTop - el.clientHeight < 600) loadMoreRef.current();
  }

  const activeFolder = folders.find((f) => f.id === ofFolderId);
  const headerTitle = activeFolder?.name ?? "All media";

  function refreshAll() {
    qc.invalidateQueries({ queryKey: ["vault-mirror-items", accountId] });
    qc.invalidateQueries({ queryKey: ["vault-lists", accountId] });
    qc.invalidateQueries({ queryKey: ["vault-cache-summary", accountId] });
  }

  async function onCollect() {
    if (!accountId || isCollecting) return;
    try {
      await startCollect(accountId);
    } catch {
      /* already running */
    }
    qc.invalidateQueries({ queryKey: ["vault-cache-summary", accountId] });
    for (let i = 0; i < 4; i++) {
      await new Promise((r) => setTimeout(r, 2500));
      qc.invalidateQueries({ queryKey: ["vault-cache-summary", accountId] });
      const st = await fetchCollectStatus(accountId).catch(() => null);
      if (st?.run && (st.run.total_seen ?? 0) > 0) {
        refreshAll();
        break;
      }
    }
  }

  async function onDescribeAll(mode: "new" | "restage" | "force" = "new") {
    if (!accountId || describeAll) return;
    if (mode !== "new") {
      const n = mode === "force" ? (plan.data?.total ?? 0) : (plan.data?.restage ?? 0);
      const est = ((promptVersion === "v2" ? 0.045 : 0.011) * n) / 100;
      const what =
        mode === "force"
          ? `Re-describe ALL ${n} items`
          : `Re-scan ${n} items still on the other prompt`;
      if (!window.confirm(`${what} with prompt ${promptVersion.toUpperCase()}?\n\n` +
          `Estimated ~$${est.toFixed(2)} of LLM spend against today's cap.`)) {
        return;
      }
    }
    setDescribeAll("starting");
    try {
      await startDescribeAll(accountId, {
        force: mode === "force",
        restage: mode === "restage",
        promptVersion,
      });
    } catch {
      setDescribeAll("");
      return;
    }
    for (let i = 0; i < 400; i++) {
      await new Promise((r) => setTimeout(r, 2500));
      const st = await fetchDescribeAllStatus(accountId).catch(() => null);
      const p = st?.progress;
      if (p) setDescribeAll(`${p.done}/${p.total}${p.capped ? " (cap hit)" : ""}`);
      if (st && !st.running) break;
      if (i % 4 === 0) qc.invalidateQueries({ queryKey: ["vault-mirror-items", accountId] });
    }
    setDescribeAll("");
    qc.invalidateQueries({ queryKey: ["vault-mirror-items", accountId] });
    qc.invalidateQueries({ queryKey: ["vault-describe-plan", accountId] });
    for (const k of ["vault-flags-review", "vault-flags-accuracy", "vault-ai-folder-plan"]) {
      qc.invalidateQueries({ queryKey: [k] });
    }
    // Offer the manual pass rather than relying on anyone remembering it
    // exists. Describing writes what the model BELIEVES; every wrong answer
    // this system has had was invisible to every automatic check and obvious
    // to a human in seconds, so the end of a sweep is exactly when to ask.
    const fin = await fetchDescribeAllStatus(accountId).catch(() => null);
    const n = fin?.progress?.needs_review ?? 0;
    if (n > 0) setNeedsReview(n);
  }

  async function onHarvest() {
    if (!accountId || harvest) return;
    setHarvest("starting");
    try {
      await startHarvestKeywords(accountId);
    } catch {
      setHarvest("");
      return;
    }
    for (let i = 0; i < 200; i++) {
      await new Promise((r) => setTimeout(r, 2000));
      const st = await fetchHarvestStatus(accountId).catch(() => null);
      const p = st?.progress;
      if (p) setHarvest(`${p.done}/${p.total} · ${p.matches} hits`);
      if (st && !st.running) break;
      if (i % 5 === 0) qc.invalidateQueries({ queryKey: ["vault-mirror-items", accountId] });
    }
    setHarvest("");
    qc.invalidateQueries({ queryKey: ["vault-mirror-items", accountId] });
  }

  function toggleSel(m: VaultMedia) {
    setSelected((prev) => {
      const n = new Set(prev);
      if (n.has(m.id)) n.delete(m.id);
      else n.add(m.id);
      return n;
    });
  }

  async function addSelectedToFolder(listId: number) {
    if (!accountId || selected.size === 0) return;
    setBusy("adding to OF…");
    try {
      await addToOfFolder(accountId, listId, [...selected]);
      setSelected(new Set());
      refreshAll();
    } finally {
      setBusy("");
    }
  }

  async function newFolderWithSelection() {
    if (!accountId) return;
    const name = window.prompt("New OF folder name:");
    if (!name?.trim()) return;
    setBusy("creating OF folder…");
    try {
      await createOfFolder(accountId, name.trim(), [...selected]);
      setSelected(new Set());
      refreshAll();
    } finally {
      setBusy("");
    }
  }

  async function describeSelected() {
    if (!accountId || selected.size === 0) return;
    const ids = [...selected];
    setBusy(`describing 0/${ids.length}`);
    for (let i = 0; i < ids.length; i++) {
      await describeMedia(accountId, ids[i]).catch(() => {});
      setBusy(`describing ${i + 1}/${ids.length}`);
    }
    setBusy("");
    setSelected(new Set());
    qc.invalidateQueries({ queryKey: ["vault-mirror-items", accountId] });
  }

  async function describeOne(m: Tile) {
    if (!accountId) return;
    setBusy("describing");
    try {
      const r = (await describeMedia(accountId, m.id)) as Tile;
      setPreview({ ...m, _ai: { ...(m._ai ?? {}), ...r } });
      qc.invalidateQueries({ queryKey: ["vault-mirror-items", accountId] });
    } finally {
      setBusy("");
    }
  }

  // After an edit save, merge the returned (effective-value) fields back into the
  // open preview so the editor reflects the override immediately, and refresh the
  // grid so the S9 overlay picks up the new description/tags.
  function onSavedItem(updated: Record<string, unknown>) {
    setPreview((prev) => (prev ? { ...prev, _ai: { ...(prev._ai ?? {}), ...updated } } : prev));
    qc.invalidateQueries({ queryKey: ["vault-mirror-items", accountId] });
  }

  async function setOrder(m: VaultMedia, value: number | null) {
    if (!accountId || ofFolderId == null) return;
    await reorderItems(accountId, ofFolderId, [{ media_id: m.id, manual_order: value }]).catch(
      () => {},
    );
    qc.invalidateQueries({ queryKey: ["vault-mirror-items", accountId] });
  }

  // ── Folder management (real OF writes) ──────────────────────────
  const [customizeOrder, setCustomizeOrder] = useState(false);
  const [localOrder, setLocalOrder] = useState<number[] | null>(null);
  const [draggedId, setDraggedId] = useState<number | null>(null);
  async function onNewFolder() {
    if (!accountId) return;
    const name = window.prompt("New OF folder name:");
    if (!name?.trim()) return;
    await createOfFolder(accountId, name.trim(), []).catch(() => {});
    refreshAll();
  }
  async function onRenameFolder(f: VaultList) {
    if (!accountId) return;
    const name = window.prompt("Rename folder:", f.name);
    if (!name?.trim() || name.trim() === f.name) return;
    await renameOfFolder(accountId, f.id, name.trim()).catch(() => {});
    refreshAll();
  }
  async function onDeleteFolder(f: VaultList) {
    if (!accountId) return;
    if (!window.confirm(`Delete folder “${f.name}”? (media stays in your vault)`)) return;
    await deleteOfFolder(accountId, f.id).catch(() => {});
    if (ofFolderId === f.id) setOfFolderId(null);
    refreshAll();
  }
  // ── Customize (drag) folder order → OF customOrder ──────────────
  function startCustomize() {
    setLocalOrder(folders.map((f) => f.id));
    setCustomizeOrder(true);
  }
  function doneCustomize() {
    setCustomizeOrder(false);
    setLocalOrder(null);
    setDraggedId(null);
  }
  function onDragOverFolder(e: ReactDragEvent, overId: number) {
    e.preventDefault();
    if (draggedId == null || draggedId === overId || !localOrder) return;
    const arr = [...localOrder];
    const from = arr.indexOf(draggedId);
    const to = arr.indexOf(overId);
    if (from < 0 || to < 0) return;
    arr.splice(from, 1);
    arr.splice(to, 0, draggedId);
    setLocalOrder(arr);
  }
  async function onDropFolder() {
    setDraggedId(null);
    if (!accountId || !localOrder) return;
    await sortOfFolders(accountId, { customOrder: localOrder }).catch(() => {});
    refreshAll();
  }

  const displayFolders =
    customizeOrder && localOrder
      ? (localOrder.map((id) => folders.find((f) => f.id === id)).filter(Boolean) as VaultList[])
      : folders;

  if (accounts.length === 0) {
    return <div className="text-sm text-fg-dim">No model accounts with a live session.</div>;
  }

  return (
    <div className="space-y-3">
      {/* top controls */}
      <div className="flex flex-wrap items-center gap-3 justify-between">
        <AccountChips accountId={accountId} onChange={setAccountId} />
        <div className="flex flex-wrap md:flex-nowrap items-center gap-2">
          <span className="text-xs text-fg-dim">
            {isCollecting ? (
              <>
                <span className="text-amber-400">●</span> collecting… {run?.total_seen ?? 0}
              </>
            ) : cachedCount > 0 ? (
              <>
                <span className="text-emerald-400">●</span> {cachedCount} cached
              </>
            ) : (
              "not collected"
            )}
          </span>
          {/* Desk-only batch sweeps. `md:contents` keeps every button a direct
              flex child at md, so the desktop row is untouched. */}
          <div className="hidden md:contents">
            <button
              type="button"
              onClick={onCollect}
              disabled={isCollecting || !accountId}
              className="px-3 py-1.5 rounded-lg text-sm border border-accent bg-accent/10 text-accent hover:bg-accent/20 disabled:opacity-50"
            >
              {isCollecting ? "Collecting…" : cachedCount > 0 ? "Re-collect" : "Collect all"}
            </button>
          </div>
          <button
            type="button"
            onClick={() => {
              setSelectMode((v) => !v);
              setSelected(new Set());
            }}
            className={cx(
              "px-3 py-1.5 rounded-lg text-sm border transition-colors",
              selectMode ? "bg-accent text-white border-accent" : "bg-bg-elev-1 text-fg-dim border-border hover:text-fg",
            )}
          >
            {selectMode ? "Selecting" : "Select"}
          </button>
          <div className="hidden md:contents">
            <button
              type="button"
              onClick={() => onDescribeAll("new")}
              disabled={!!describeAll || cachedCount === 0}
              className="px-3 py-1.5 rounded-lg text-sm border border-emerald-500/50 bg-emerald-500/10 text-emerald-400 hover:bg-emerald-500/20 disabled:opacity-50"
              title="Describe every un-described item (background)"
            >
              {describeAll
                ? `Describing ${describeAll}`
                : `Describe all${plan.data?.undescribed ? ` (${plan.data.undescribed})` : ""}`}
            </button>
            {/* Which bake-off prompt the sweep runs. B is the default and the only
                one that emits the structured taxonomy auto-foldering needs; A is
                ~4× cheaper for a rough first pass over a huge vault. */}
            <select
              value={promptVersion}
              onChange={(e) => setPromptVersion(e.target.value as "v1" | "v2")}
              disabled={!!describeAll}
              title="Describe prompt variant"
              className="px-2 py-1.5 rounded-lg text-sm bg-bg-elev-1 border border-border text-fg-dim"
            >
              <option value="v2">B · rich</option>
              <option value="v1">A · fast</option>
            </select>
            {!!plan.data?.restage && !describeAll && (
              <button
                type="button"
                onClick={() => onDescribeAll("restage")}
                disabled={cachedCount === 0}
                className="px-3 py-1.5 rounded-lg text-sm border border-amber-500/50 bg-amber-500/10 text-amber-400 hover:bg-amber-500/20 disabled:opacity-50"
                title="Re-describe items that were described with the OTHER prompt version. Resumable — already-current rows are skipped."
              >
                Re-scan old ({plan.data.restage})
              </button>
            )}
            <button
              type="button"
              onClick={onHarvest}
              disabled={!!harvest || cachedCount === 0}
              className="px-3 py-1.5 rounded-lg text-sm border border-sky-500/50 bg-sky-500/10 text-sky-400 hover:bg-sky-500/20 disabled:opacity-50"
              title="Run OF's own search across ~50 selling keywords and fold into local search"
            >
              {harvest ? `Harvesting ${harvest}` : "Harvest OF (50)"}
            </button>
            <button
              type="button"
              onClick={() => setShowDupes(true)}
              disabled={cachedCount === 0}
              className="px-3 py-1.5 rounded-lg text-sm border border-rose-500/50 bg-rose-500/10 text-rose-400 hover:bg-rose-500/20 disabled:opacity-50"
              title="Find re-uploaded copies and remove them from the OF vault (review + confirm first)"
            >
              Duplicates
            </button>
            <button
              type="button"
              onClick={() => setShowAiFolders(true)}
              disabled={cachedCount === 0}
              className="px-3 py-1.5 rounded-lg text-sm border border-violet-500/50 bg-violet-500/10 text-violet-300 hover:bg-violet-500/20 disabled:opacity-50"
              title="Group the vault into shoots and send lanes — preview first, nothing is created until you confirm"
            >
              Build AI folders
            </button>
            <button
              type="button"
              onClick={() => setShowDisputes(true)}
              disabled={cachedCount === 0}
              className="px-3 py-1.5 rounded-lg text-sm border border-amber-500/50 bg-amber-500/10 text-amber-300 hover:bg-amber-500/20 disabled:opacity-50"
              title="Items where the two vision passes disagree — pick the reading that matches the picture. Your choice is locked against re-runs."
            >
              Fix disagreements
            </button>
          </div>
        </div>
      </div>

      {/* selection action bar */}
      {selectMode && selected.size > 0 && (
        <div className="flex flex-wrap items-center gap-2 rounded-lg border border-accent/40 bg-accent/5 px-3 py-2">
          <span className="text-sm font-medium">{selected.size} selected</span>
          <button
            type="button"
            onClick={newFolderWithSelection}
            disabled={!!busy}
            className="px-2.5 py-1 rounded-md text-xs border border-border bg-bg-elev-1 hover:text-fg"
          >
            + New OF folder with these
          </button>
          {folders.length > 0 && (
            <select
              onChange={(e) => e.target.value && addSelectedToFolder(Number(e.target.value))}
              defaultValue=""
              disabled={!!busy}
              className="px-2 py-1 rounded-md text-xs bg-bg-elev-1 border border-border"
            >
              <option value="">Add to OF folder…</option>
              {folders.map((f) => (
                <option key={f.id} value={f.id}>
                  {f.name}
                </option>
              ))}
            </select>
          )}
          <button
            type="button"
            onClick={describeSelected}
            disabled={!!busy}
            className="px-2.5 py-1 rounded-md text-xs border border-emerald-500/50 bg-emerald-500/10 text-emerald-400 hover:bg-emerald-500/20"
          >
            Describe selected
          </button>
          {busy && <span className="text-xs text-fg-dim">{busy}</span>}
          <button
            type="button"
            onClick={() => setSelected(new Set())}
            className="px-2 py-1 rounded-md text-xs text-fg-dim hover:text-fg"
          >
            Clear
          </button>
        </div>
      )}

      {/* Fixed-height two-pane: each side scrolls on its OWN, so only the media
          grid moves (with its own bottom-of-list infinite load) and the folder
          rail stays put — the page itself barely scrolls. */}
      <div className="flex flex-col md:flex-row items-stretch rounded-xl border border-border overflow-hidden h-[70vh] md:h-[74vh]">
        {/* LEFT rail — OF folders (independently scrollable) */}
        <aside className="w-full md:w-56 shrink-0 max-h-44 md:max-h-none border-b md:border-b-0 md:border-r border-border bg-bg-elev-1/40 overflow-y-auto">
          <button
            type="button"
            onClick={() => setOfFolderId(null)}
            className={cx(
              "w-full text-left px-3 py-2.5 border-b border-border transition-colors",
              ofFolderId === null ? "bg-accent/10 text-fg" : "text-fg-dim hover:bg-bg-elev-1",
            )}
          >
            <div className="text-sm font-medium">All media</div>
            <div className="text-[11px] text-fg-dim mt-0.5">
              {cachedCount > 0 ? `${cachedCount} cached` : "everything"}
            </div>
          </button>
          <div className="px-3 py-1.5 flex items-center justify-between border-b border-border relative">
            <span className="text-[10px] uppercase tracking-wide text-fg-dim">Folders</span>
            <div className="flex items-center gap-1">
              <button
                type="button"
                onClick={onNewFolder}
                title="New folder"
                className="w-6 h-6 grid place-items-center rounded-md text-fg-dim hover:text-fg hover:bg-bg-elev-1 text-base leading-none"
              >
                +
              </button>
              <button
                type="button"
                onClick={startCustomize}
                title="Customize folder order (drag)"
                className={cx(
                  // HTML5 drag never fires on touch, and while the mode is on a
                  // folder tap stops opening the folder — so hide it on phone.
                  "hidden md:grid w-6 h-6 place-items-center rounded-md hover:bg-bg-elev-1",
                  customizeOrder ? "text-accent" : "text-fg-dim hover:text-fg",
                )}
              >
                ⇅
              </button>
            </div>
          </div>
          {customizeOrder && (
            <div className="px-3 py-1.5 flex items-center justify-between bg-accent/10 border-b border-border text-xs">
              <span className="text-accent">Drag folders to reorder</span>
              <button
                type="button"
                onClick={doneCustomize}
                className="px-2 py-0.5 rounded bg-accent text-white text-xs"
              >
                Done
              </button>
            </div>
          )}
          {lists.isLoading ? (
            <div className="px-3 py-3 text-xs text-fg-dim">Loading…</div>
          ) : displayFolders.length === 0 ? (
            <div className="px-3 py-2 text-[11px] text-fg-dim">Select media → New folder</div>
          ) : (
            displayFolders.map((f) => (
              <div
                key={f.id}
                role="button"
                tabIndex={0}
                draggable={customizeOrder}
                onDragStart={customizeOrder ? () => setDraggedId(f.id) : undefined}
                onDragOver={customizeOrder ? (e) => onDragOverFolder(e, f.id) : undefined}
                onDrop={customizeOrder ? onDropFolder : undefined}
                onClick={() => {
                  if (!customizeOrder) setOfFolderId(f.id);
                }}
                className={cx(
                  "group w-full text-left px-3 py-2 border-b border-border/60 transition-colors flex items-center justify-between gap-2",
                  customizeOrder ? "cursor-grab active:cursor-grabbing" : "cursor-pointer",
                  draggedId === f.id ? "opacity-50" : "",
                  ofFolderId === f.id && !customizeOrder ? "bg-accent/10 text-fg" : "text-fg-dim hover:bg-bg-elev-1",
                )}
              >
                <div className="flex items-center gap-2 min-w-0">
                  {customizeOrder && <span className="text-fg-dim select-none text-base leading-none">⣿</span>}
                  <div className="min-w-0">
                    <div className="text-sm font-medium truncate">📁 {f.name}</div>
                    <div className="text-[11px] text-fg-dim mt-0.5">{folderCounts(f)}</div>
                  </div>
                </div>
                {!customizeOrder && (
                  <div className="flex items-center gap-1 opacity-100 md:opacity-0 md:group-hover:opacity-100 transition-opacity shrink-0">
                    <button
                      type="button"
                      onClick={(e) => {
                        e.stopPropagation();
                        onRenameFolder(f);
                      }}
                      title="Rename"
                      className="w-10 h-10 md:w-6 md:h-6 grid place-items-center rounded-md hover:bg-bg text-fg-dim hover:text-fg text-xs"
                    >
                      ✏️
                    </button>
                    <button
                      type="button"
                      onClick={(e) => {
                        e.stopPropagation();
                        onDeleteFolder(f);
                      }}
                      title="Delete folder"
                      className="w-10 h-10 md:w-6 md:h-6 grid place-items-center rounded-md hover:bg-red-500/20 text-fg-dim hover:text-red-400 text-xs"
                    >
                      🗑
                    </button>
                  </div>
                )}
              </div>
            ))
          )}
        </aside>

        {/* RIGHT grid */}
        <section className="flex-1 min-w-0 flex flex-col">
          <div className="px-3 py-2 border-b border-border flex flex-wrap items-center gap-2 justify-between sticky top-0 bg-bg/80 backdrop-blur z-10">
            <div className="text-sm font-semibold uppercase tracking-wide">
              {headerTitle}
              {useLocal && <span className="ml-2 text-[10px] text-emerald-400">● local</span>}
              {usingFolder && <span className="ml-2 text-[10px] text-fg-dim">(set # to reorder)</span>}
            </div>
            <div className="flex items-center gap-2 w-full md:w-auto">
              <input
                value={searchRaw}
                onChange={(e) => setSearchRaw(e.target.value)}
                placeholder={useLocal ? "Search tags (instant)…" : "Search…"}
                className="px-3 py-1.5 rounded-lg text-base md:text-sm bg-bg-elev-1 border border-border focus:border-accent outline-none w-48 max-md:w-auto max-md:flex-1 max-md:min-w-0"
              />
              <select
                value={sort}
                onChange={(e) => setSort(e.target.value as "newest" | "oldest")}
                className="px-2 py-1.5 rounded-lg text-sm bg-bg-elev-1 border border-border outline-none"
              >
                <option value="newest">Newest</option>
                <option value="oldest">Oldest</option>
              </select>
            </div>
          </div>

          <div className="px-3 py-2 border-b border-border flex items-center gap-1.5">
            {TYPE_TABS.map((t) => (
              <button
                key={t.key}
                type="button"
                onClick={() => setType(t.key)}
                className={cx(
                  "px-3 py-1 rounded-full text-xs border transition-colors",
                  type === t.key
                    ? "bg-accent text-white border-accent"
                    : "bg-bg-elev-1 text-fg-dim border-border hover:text-fg hover:border-fg-dim",
                )}
              >
                {t.label}
              </button>
            ))}
          </div>

          <div ref={scrollRef} onScroll={onGridScroll} className="p-3 flex-1 min-h-0 overflow-y-auto">
            {showLoading ? (
              <div className="text-sm text-fg-dim py-10 text-center">Loading…</div>
            ) : items.length === 0 ? (
              <div className="text-sm text-fg-dim py-10 text-center">
                {query ? `No media matching “${query}”.` : "No media here."}
              </div>
            ) : (
              <>
                <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 gap-2">
                  {items.map((m) => (
                    <VaultTile
                      key={m.id}
                      media={m}
                      accountId={accountId}
                      onClick={setPreview}
                      onPlay={setPlayMedia}
                      selected={preview?.id === m.id}
                      selectMode={selectMode}
                      checked={selected.has(m.id)}
                      onCheck={toggleSel}
                      showOrder={usingFolder}
                      onOrder={setOrder}
                    />
                  ))}
                </div>
                <div ref={sentinelRef} className="h-8" />
                <div className="text-center text-xs text-fg-dim pt-1 pb-2">
                  {pageFetching ? (
                    "Loading…"
                  ) : src.hasMore ? (
                    // Always clickable — auto-load is the happy path, but the
                    // user must never be stranded staring at a dead hint line.
                    <button
                      type="button"
                      onClick={() => src.loadMore()}
                      className="px-3 py-1.5 rounded-lg border border-border bg-bg-elev-1 text-fg-dim hover:text-fg hover:border-fg-dim transition-colors"
                    >
                      {pageError ? "Couldn’t load more · retry" : `${items.length} shown · load more`}
                    </button>
                  ) : (
                    `${items.length} shown`
                  )}
                </div>
              </>
            )}
          </div>
        </section>
      </div>

      {preview && !selectMode && accountId && (
        // Right-side slide-over, deliberately WITHOUT a dimming backdrop so the
        // grid stays visible AND clickable behind it — click another tile and the
        // drawer just swaps to that item.
        <aside
          ref={drawerRef}
          className="fixed inset-0 md:inset-y-0 md:left-auto md:right-0 z-40 h-full w-full md:w-[430px] flex flex-col border-l border-border bg-panel/95 backdrop-blur-sm shadow-2xl pt-[env(safe-area-inset-top)] md:pt-0 pb-[env(safe-area-inset-bottom)] md:pb-0"
        >
          <header className="flex items-center gap-2 px-3 py-2 border-b border-border shrink-0">
            <div className="text-sm font-medium truncate">
              #{preview.id} · {preview.type}
              {preview.duration ? ` · ${Math.round(preview.duration)}s` : ""}
            </div>
            <div className="ml-auto flex items-center gap-2">
              <button
                type="button"
                onClick={() => describeOne(preview)}
                disabled={!!busy}
                className="px-2.5 py-1 rounded-md text-xs border border-emerald-500/50 bg-emerald-500/10 text-emerald-400 hover:bg-emerald-500/20 disabled:opacity-50"
              >
                {busy === "describing"
                  ? "Describing…"
                  : (preview._ai as { describe_status?: string } | undefined)?.describe_status === "described"
                  ? "Re-describe"
                  : "Describe"}
              </button>
              <button
                type="button"
                onClick={() => setPreview(null)}
                aria-label="Close details"
                title="Close (Esc)"
                className="w-9 h-9 md:w-7 md:h-7 grid place-items-center rounded-md border border-border text-fg-dim hover:text-fg hover:bg-bg-elev-1"
              >
                ✕
              </button>
            </div>
          </header>
          {/* The editor owns the order — action bar (sticky) → media → the
              fields you act on → the rest below the fold. */}
          <div className="flex-1 min-h-0 overflow-y-auto p-3 space-y-3">
            <VaultItemEditor
              key={preview.id}
              media={preview}
              accountId={accountId}
              onSaved={onSavedItem}
              mediaSlot={
                <VaultPreviewMedia
                  key={`pv-${preview.id}`}
                  media={preview}
                  accountId={accountId}
                  onExpand={() => setPlayMedia(preview)}
                />
              }
              deleteSlot={
                <VaultDeleteButton
                  key={`del-${preview.id}`}
                  media={preview}
                  accountId={accountId}
                  onDeleted={() => {
                    setPreview(null);
                    qc.invalidateQueries({ queryKey: ["vault-mirror-items", accountId] });
                  }}
                />
              }
            />
          </div>
        </aside>
      )}
      {playMedia && (
        <VaultVideoPreview
          media={playMedia}
          accountId={accountId}
          onClose={() => setPlayMedia(null)}
        />
      )}
      {showDupes && accountId && (
        <VaultDuplicatesModal accountId={accountId} onClose={() => setShowDupes(false)} />
      )}
      {needsReview > 0 && !showFlagsReview && (
        <div className="mx-4 mb-2 rounded-lg border border-emerald-500/50 bg-emerald-500/10 px-3 py-2 flex items-center justify-between gap-3">
          <p className="text-[11px] text-emerald-200">
            Described and flagged. <b>{needsReview}</b>{" "}
            {needsReview === 1 ? "item looks" : "items look"} worth your eye — the
            model disagrees with itself on them, or cannot say what is covering her.
          </p>
          <div className="flex gap-2 shrink-0">
            <button
              type="button"
              onClick={() => setShowFlagsReview(true)}
              className="px-2.5 py-1 rounded-md text-[11px] font-medium border border-emerald-500/60 text-emerald-200 hover:bg-emerald-500/20"
            >
              Check them
            </button>
            <button
              type="button"
              onClick={() => setNeedsReview(0)}
              className="px-2 py-1 rounded-md text-[11px] text-fg-dim hover:text-fg"
            >
              Later
            </button>
          </div>
        </div>
      )}

      {showFlagsReview && accountId && (
        <VaultFlagsReviewModal
          accountId={accountId}
          onClose={() => setShowFlagsReview(false)}
        />
      )}

      {showDisputes && accountId && (
        <VaultDisputesModal accountId={accountId} onClose={() => setShowDisputes(false)} />
      )}

      {showAiFolders && accountId && (
        <VaultAiFoldersModal accountId={accountId} onClose={() => setShowAiFolders(false)} />
      )}
    </div>
  );
}

/** Media block in the details drawer.
 *
 *  A selected video plays INLINE with native controls (same `.vault-video`
 *  styling the full-screen player uses), so watching a clip never leaves the
 *  grid. The ⤢ button hands off to the picker's `VaultVideoPreview` for
 *  full-screen. DRM videos have no progressive mp4 to play, so they fall back to
 *  the still + a ▶ that opens VaultVideoPreview's poster-frame lightbox — the
 *  same behaviour as the picker. Photos just render. */
function VaultPreviewMedia({
  media: m,
  accountId,
  onExpand,
}: {
  media: VaultMedia;
  accountId: string;
  onExpand: () => void;
}) {
  const isVideo = m.type === "video";
  const rawSrc = isVideo ? progressiveVideoSrc(m) : null;
  const drmOnly = isVideo && isDrmOnlyVideo(m);
  const playable = isVideo && !!rawSrc && !drmOnly;
  const idNum = Number(m.id);
  const [zoom, setZoom] = useState(false);
  // A PHOTO shows the FULL FRAME from the permanent cache — not `_thumb`, which
  // is a 300x300 centre-crop that lops the top and bottom off a 3:4 portrait and
  // hides edge-of-frame detail. A VIDEO keeps its poster (the square is fine as a
  // play affordance; ⤢ opens the real player).
  const photoFull = !isVideo && Number.isFinite(idNum) ? mirrorFullSrc(accountId, idNum) : null;
  const still =
    photoFull ||
    (m as unknown as { _thumb?: string })._thumb ||
    proxyImage(
      m.files?.full?.url || m.files?.preview?.url || m.files?.thumb?.url,
      accountId,
    );

  return (
    <div className="relative w-full rounded-lg overflow-hidden bg-black">
      {playable ? (
        <video
          src={proxyImage(rawSrc, accountId)}
          poster={still || undefined}
          controls
          loop
          playsInline
          className="vault-video w-full max-h-[30vh] object-contain bg-black"
        />
      ) : (
        <>
          {still && (
            // eslint-disable-next-line @next/next/no-img-element
            <img
              src={still}
              alt=""
              aria-hidden={isVideo}
              onClick={isVideo ? undefined : () => setZoom(true)}
              title={isVideo ? undefined : "Click to view full size"}
              className={`w-full max-h-[30vh] object-contain${isVideo ? "" : " cursor-zoom-in"}`}
            />
          )}
          {isVideo && (
            <button
              type="button"
              onClick={onExpand}
              title={drmOnly ? "DRM — show preview frames" : "Preview video"}
              aria-label="Preview video"
              className="absolute inset-0 grid place-items-center group"
            >
              <span className="w-14 h-14 rounded-full bg-black/60 border border-white/70 grid place-items-center text-white text-xl pl-1 transition-colors group-hover:bg-black/85">
                ▶
              </span>
            </button>
          )}
        </>
      )}
      <button
        type="button"
        onClick={isVideo ? onExpand : () => setZoom(true)}
        title="Full screen"
        aria-label="Full screen"
        className="absolute top-1 right-1 px-1.5 py-0.5 rounded bg-black/60 hover:bg-black/85 text-white text-xs leading-none"
      >
        ⤢
      </button>
      {zoom && Number.isFinite(idNum) && (
        <ImageLightbox accountId={accountId} mediaId={idNum} onClose={() => setZoom(false)} />
      )}
    </div>
  );
}

/** Remove-from-vault control. Two-step by design: this calls OF's own "Remove
 *  from vault", and OF exposes NO unhide — so a stray click must never be able
 *  to take media off the vault. The copy says exactly what happens (hidden, not
 *  destroyed; sent PPVs keep working) so the confirm is an informed one. */
function VaultDeleteButton({
  media,
  accountId,
  onDeleted,
}: {
  media: VaultMedia;
  accountId: string;
  onDeleted: () => void;
}) {
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  async function doDelete() {
    setBusy(true);
    setErr("");
    try {
      await hideVaultItem(accountId, media.id);
      onDeleted();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "remove failed");
      setBusy(false);
    }
  }

  if (!confirming) {
    return (
      <button
        type="button"
        onClick={() => setConfirming(true)}
        className="px-3 py-1.5 rounded-md text-sm border border-rose-500/50 bg-rose-500/10 text-rose-400 hover:bg-rose-500/20"
      >
        Remove from vault
      </button>
    );
  }
  return (
    <div className="w-full rounded-md border border-rose-500/50 bg-rose-500/10 p-2.5 space-y-2">
      <div className="text-xs text-rose-200 leading-snug">
        Remove <b>#{media.id}</b> from the OnlyFans vault? This uses OF&rsquo;s own
        “Remove from vault”, so it is <b>hidden, not destroyed</b> — already-sent
        PPVs and live posts keep working. <b>OnlyFans offers no undo.</b>
      </div>
      {err && <div className="text-xs text-rose-300">{err}</div>}
      <div className="flex gap-2">
        <button
          type="button"
          disabled={busy}
          onClick={doDelete}
          className="flex-1 px-3 py-1.5 rounded-md text-sm border border-rose-500/60 bg-rose-500/20 text-rose-200 hover:bg-rose-500/30 disabled:opacity-50"
        >
          {busy ? "Removing…" : "Yes, remove"}
        </button>
        <button
          type="button"
          disabled={busy}
          onClick={() => setConfirming(false)}
          className="px-3 py-1.5 rounded-md text-sm border border-border hover:bg-bg-elev-1 disabled:opacity-50"
        >
          Cancel
        </button>
      </div>
    </div>
  );
}

/** Click-to-edit detail/editor for one tile: the FULL description, short tags,
 *  explicitness tier, and suggested caption/script — all editable. Save writes
 *  ONLY the changed fields through the override+lock endpoint (contract §6c), so
 *  a later Describe rerun can't clobber what the operator touched. */
/** Read-only view of the V2 describe taxonomy (`_ai.fields`, projected straight
 *  from `ai_fields_json`). These 20-odd fields have always been STORED — acts,
 *  clothing_state, beats, primary_folder, penetration, camera, confidence — but
 *  until now nothing rendered them, so a described item looked like it only had
 *  a sentence and some tags. `beats` is the clip's ordered progression and is
 *  the reason a long video is worth describing at all, so it leads.
 *
 *  Not editable: the editable, lockable fields are the real columns above. This
 *  is the raw AI read-out, shown so you can see what the model actually found. */
// What the model found, ranked by what an operator actually acts on. PRIMARY is
// shown expanded; everything else sits below the fold behind a toggle. `beats`
// leads because it's the clip's ordered progression — the whole reason a long
// video is worth describing.
const AI_PRIMARY = [
  "acts",
  "clothing_state",
  "penetration",
  "position",
  "primary_folder",
  "explicitness",
] as const;
// Internal bookkeeping stamps, not model output.
const AI_HIDE = new Set(["beats", "description", "tags", "_prompt_version", "_frames"]);

function fmtAi(v: unknown): string {
  if (Array.isArray(v)) return v.map(String).join(", ");
  if (v === true) return "yes";
  if (v === false) return "no";
  return String(v);
}

function AiRows({ entries }: { entries: [string, unknown][] }) {
  return (
    <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-[12px] m-0">
      {entries.map(([k, v]) => (
        <div key={k} className="contents">
          <dt className="text-fg-dim whitespace-nowrap">{k.replace(/_/g, " ")}</dt>
          <dd className="m-0 text-fg break-words">{fmtAi(v)}</dd>
        </div>
      ))}
    </dl>
  );
}

/** The V2 describe taxonomy (`_ai.fields`, projected from `ai_fields_json`).
 *  These ~20 fields have always been STORED; until now nothing rendered them,
 *  so a described item looked like it only had a sentence and some tags.
 *  Read-only — the editable, lockable fields are the real columns. */
function AiFieldsPanel({ fields }: { fields?: Record<string, unknown> }) {
  const [open, setOpen] = useState(false);
  if (!fields || Object.keys(fields).length === 0) return null;

  const beats = Array.isArray(fields.beats) ? (fields.beats as unknown[]) : [];
  const live = Object.entries(fields).filter(
    ([k, v]) =>
      !AI_HIDE.has(k) && v !== null && v !== "" && v !== "none" &&
      !(Array.isArray(v) && v.length === 0),
  );
  const primary = live.filter(([k]) => (AI_PRIMARY as readonly string[]).includes(k));
  const rest = live.filter(([k]) => !(AI_PRIMARY as readonly string[]).includes(k));

  return (
    <div className="space-y-2">
      {beats.length > 0 && (
        <div>
          <div className="text-[11px] uppercase tracking-wide text-fg-dim mb-1">
            What happens ({beats.length} steps)
          </div>
          <ol className="list-decimal pl-4 text-[12.5px] text-fg space-y-0.5 m-0">
            {beats.map((b, i) => (
              <li key={i}>{String(b)}</li>
            ))}
          </ol>
        </div>
      )}
      {primary.length > 0 && <AiRows entries={primary} />}
      {rest.length > 0 && (
        <div className="rounded-lg border border-border bg-bg-elev-1/40">
          <button
            type="button"
            onClick={() => setOpen((v) => !v)}
            className="w-full flex items-center gap-2 px-2.5 py-1.5 text-[11px] uppercase tracking-wide text-fg-dim hover:text-fg"
          >
            <span>{open ? "▾" : "▸"}</span>
            <span>More AI detail</span>
            <span className="text-fg-dim/70 normal-case tracking-normal">
              {rest.length} fields
            </span>
          </button>
          {open && (
            <div className="px-2.5 pb-2.5">
              <AiRows entries={rest} />
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function VaultItemEditor({
  media,
  accountId,
  onSaved,
  mediaSlot,
  deleteSlot,
}: {
  media: Tile;
  accountId: string;
  onSaved: (updated: Record<string, unknown>) => void;
  /** The preview image/player. Rendered BELOW the action bar so Save and Remove
   *  are reachable without scrolling past a tall video. */
  mediaSlot?: React.ReactNode;
  deleteSlot?: React.ReactNode;
}) {
  const ai = (media._ai ?? {}) as Record<string, unknown>;
  const described =
    !!ai.description || !!ai.video_description || !!(ai.tags as unknown[] | undefined)?.length;

  const priceCents = ai.suggested_price_cents as number | undefined;

  const init = {
    desc: (ai.description || ai.video_description || "") as string,
    tags: (((ai.tags as string[] | undefined) ?? []) as string[]).join(", "),
    tier: (ai.explicitness_tier as string | undefined) ?? "",
    cap: (ai.suggested_caption as string | undefined) ?? "",
    script: (ai.suggested_script as string | undefined) ?? "",
    price: typeof priceCents === "number" ? (priceCents / 100).toFixed(2) : "",
    // The exposure flags. They decide the folder, the price band and whether
    // an item may be mass-sent, so an operator who spots a wrong one while
    // editing a caption should be able to fix it here rather than hunting for
    // the review modal.
    breasts: (ai.breasts_vis as string | undefined) ?? "",
    vulva: (ai.vulva_vis as string | undefined) ?? "",
    anus: (ai.anus_vis as string | undefined) ?? "",
  };

  const [desc, setDesc] = useState(init.desc);
  const [tags, setTags] = useState(init.tags);
  const [tier, setTier] = useState(init.tier);
  const [cap, setCap] = useState(init.cap);
  const [script, setScript] = useState(init.script);
  const [priceStr, setPriceStr] = useState(init.price);
  const [breasts, setBreasts] = useState(init.breasts);
  const [vulva, setVulva] = useState(init.vulva);
  const [anus, setAnus] = useState(init.anus);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState<string[] | null>(null);
  const [err, setErr] = useState("");
  const [locked, setLocked] = useState<Set<string>>(new Set());

  // Only fields whose value actually changed are sent (and thus locked).
  const changed: Record<string, unknown> = {};
  if (desc !== init.desc) changed.description = desc;
  if (tags !== init.tags) changed.tags = tags;
  if (tier !== init.tier) changed.explicitness_tier = tier;
  if (cap !== init.cap) changed.suggested_caption = cap;
  if (script !== init.script) changed.suggested_script = script;
  // Dollars in the input → cents on the wire. Blank clears the price; garbage is
  // simply not sent, so a typo can't wipe a real price.
  if (breasts !== init.breasts) changed.breasts_vis = breasts;
  if (vulva !== init.vulva) changed.vulva_vis = vulva;
  if (anus !== init.anus) changed.anus_vis = anus;
  const priceTrimmed = priceStr.trim();
  if (priceTrimmed !== init.price) {
    if (priceTrimmed === "") {
      changed.suggested_price_cents = null;
    } else {
      const n = Number(priceTrimmed.replace(/[^0-9.]/g, ""));
      if (Number.isFinite(n)) changed.suggested_price_cents = Math.round(n * 100);
    }
  }
  const dirty = Object.keys(changed).length > 0;

  async function onSave() {
    if (!dirty || saving) return;
    setSaving(true);
    setErr("");
    setSaved(null);
    try {
      const res = await saveVaultItem(accountId, media.id, changed);
      setLocked(new Set(res.locked ?? []));
      setSaved(Object.keys(changed));
      onSaved(res.item ?? {});
    } catch (e) {
      setErr(e instanceof Error ? e.message : "save failed");
    } finally {
      setSaving(false);
    }
  }

  const lockBadge = (field: string) =>
    locked.has(field) ? <span className="ml-1 text-[10px] text-amber-400" title="Locked — a Describe rerun won't overwrite this">🔒</span> : null;

  const label = "text-[11px] uppercase tracking-wide text-fg-dim";
  const inputCls =
    "w-full px-2 py-1.5 rounded-md text-base md:text-sm bg-bg-elev-1 border border-border focus:border-accent outline-none";

  return (
    <div className="space-y-2.5">
      {/* Actions first, and sticky: Save and Remove stay reachable no matter how
          far down the fields you've scrolled, and you never have to scroll PAST
          a tall video to reach them. */}
      <div className="sticky top-0 z-10 -mx-3 px-3 py-2 bg-panel/95 backdrop-blur-sm border-b border-border flex flex-wrap items-center gap-2">
        <button
          type="button"
          onClick={onSave}
          disabled={!dirty || saving}
          className="px-3 py-1.5 rounded-lg text-sm border border-accent bg-accent/10 text-accent hover:bg-accent/20 disabled:opacity-40"
        >
          {saving ? "Saving…" : "Save & lock"}
        </button>
        {deleteSlot}
        <span className="ml-auto text-right">
          {saved && !err && (
            <span className="text-xs text-emerald-400">
              Saved — locked {saved.length} field{saved.length === 1 ? "" : "s"}
            </span>
          )}
          {err && <span className="text-xs text-red-400">{err}</span>}
        </span>
      </div>
      {mediaSlot}
      {!described && (
        <div className="text-xs text-fg-dim">Not described yet — click Describe, or write fields below.</div>
      )}
      <div>
        <div className={label}>Description{lockBadge("description")}</div>
        <textarea
          value={desc}
          onChange={(e) => setDesc(e.target.value)}
          rows={3}
          placeholder="Full description…"
          className={cx(inputCls, "mt-1 resize-y")}
        />
      </div>
      {/* The AI read-out sits directly under the description — it's what you
          check the description against, and it drives foldering. */}
      <AiFieldsPanel fields={ai.fields as Record<string, unknown> | undefined} />
      <div>
        <div className={label}>Tags{lockBadge("tags")}</div>
        <input
          value={tags}
          onChange={(e) => setTags(e.target.value)}
          placeholder="comma, separated, tags"
          className={cx(inputCls, "mt-1")}
        />
      </div>
      <div>
        <div className={label}>
          What is on show — decides the folder, the price and whether it can be
          mass-sent
        </div>
        <div className="flex gap-2 mt-1">
          {(
            [
              ["breasts", breasts, setBreasts, "breasts_vis"],
              ["vulva", vulva, setVulva, "vulva_vis"],
              ["anus", anus, setAnus, "anus_vis"],
            ] as const
          ).map(([name, val, set, field]) => (
            <div key={name} className="flex-1">
              <div className="text-[10px] text-fg-dim">
                {name}
                {lockBadge(field)}
              </div>
              <select
                value={VIS_OPTIONS.includes(val) ? val : ""}
                onChange={(e) => set(e.target.value)}
                className={cx(inputCls, "mt-0.5")}
              >
                <option value="">—</option>
                {VIS_OPTIONS.map((v) => (
                  <option key={v} value={v}>
                    {v === "not_in_frame" ? "not in frame" : v}
                  </option>
                ))}
              </select>
            </div>
          ))}
        </div>
      </div>
      <div className="flex gap-2">
        <div className="flex-1">
          <div className={label}>Tier{lockBadge("explicitness_tier")}</div>
          <select
            value={TIER_OPTIONS.includes(tier) ? tier : ""}
            onChange={(e) => setTier(e.target.value)}
            className={cx(inputCls, "mt-1")}
          >
            <option value="">—</option>
            {TIER_OPTIONS.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>
        </div>
        <div className="flex-1">
          <div className={label}>Price{lockBadge("suggested_price_cents")}</div>
          <div className="relative mt-1">
            <span className="absolute left-2 top-1/2 -translate-y-1/2 text-base md:text-sm text-fg-dim pointer-events-none">
              $
            </span>
            <input
              value={priceStr}
              onChange={(e) => setPriceStr(e.target.value)}
              inputMode="decimal"
              placeholder="—"
              title="Your price for this media. Attaching media in chat prices the message at the HIGHEST price among the selected items."
              className={cx(inputCls, "pl-5")}
            />
          </div>
        </div>
      </div>
      <div>
        <div className={label}>Suggested caption{lockBadge("suggested_caption")}</div>
        <input
          value={cap}
          onChange={(e) => setCap(e.target.value)}
          placeholder="One-line PPV/DM caption…"
          className={cx(inputCls, "mt-1")}
        />
      </div>
      <div>
        <div className={label}>Suggested script{lockBadge("suggested_script")}</div>
        <textarea
          value={script}
          onChange={(e) => setScript(e.target.value)}
          rows={2}
          placeholder="Longer sell copy the AI Chatter can send…"
          className={cx(inputCls, "mt-1 resize-y")}
        />
      </div>
      {!dirty && !saved && !err && (
        <div className="text-xs text-fg-dim pt-0.5">Edit a field to enable save.</div>
      )}
    </div>
  );
}
