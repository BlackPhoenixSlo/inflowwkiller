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
  startHarvestKeywords,
  useMirrorItems,
  useVaultCacheSummary,
} from "@/hooks/useVaultCache";
import { type VaultList, type VaultMedia } from "@/lib/relay";

type MediaType = "all" | "photo" | "video" | "gif" | "audio";
type Tile = VaultMedia & { _ai?: Record<string, unknown> };

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
  const [selectMode, setSelectMode] = useState(false);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [busy, setBusy] = useState<string>("");
  const [describeAll, setDescribeAll] = useState<string>("");
  const [harvest, setHarvest] = useState<string>("");
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
  loadMoreRef.current = src.loadMore;
  canLoadRef.current = src.hasMore && !src.isFetching;

  useEffect(() => {
    const el = sentinelRef.current;
    if (!el) return;
    // Observe scroll INSIDE the media pane (scrollRef), not the page viewport —
    // the grid is its own scroll container now. `items.length` in the deps
    // re-attaches the observer once the sentinel mounts after the first load
    // (it doesn't exist while isLoading), otherwise nothing fires at the bottom.
    const obs = new IntersectionObserver(
      (entries) => {
        if (entries[0]?.isIntersecting && canLoadRef.current) loadMoreRef.current();
      },
      { root: scrollRef.current, rootMargin: "600px" },
    );
    obs.observe(el);
    return () => obs.disconnect();
  }, [accountId, ofFolderId, type, query, sort, items.length]);

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

  async function onDescribeAll() {
    if (!accountId || describeAll) return;
    setDescribeAll("starting");
    try {
      await startDescribeAll(accountId, false);
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
        <div className="flex items-center gap-2">
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
          <button
            type="button"
            onClick={onCollect}
            disabled={isCollecting || !accountId}
            className="px-3 py-1.5 rounded-lg text-sm border border-accent bg-accent/10 text-accent hover:bg-accent/20 disabled:opacity-50"
          >
            {isCollecting ? "Collecting…" : cachedCount > 0 ? "Re-collect" : "Collect all"}
          </button>
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
          <button
            type="button"
            onClick={onDescribeAll}
            disabled={!!describeAll || cachedCount === 0}
            className="px-3 py-1.5 rounded-lg text-sm border border-emerald-500/50 bg-emerald-500/10 text-emerald-400 hover:bg-emerald-500/20 disabled:opacity-50"
            title="Describe every un-described item (background, cheap)"
          >
            {describeAll ? `Describing ${describeAll}` : "Describe all"}
          </button>
          <button
            type="button"
            onClick={onHarvest}
            disabled={!!harvest || cachedCount === 0}
            className="px-3 py-1.5 rounded-lg text-sm border border-sky-500/50 bg-sky-500/10 text-sky-400 hover:bg-sky-500/20 disabled:opacity-50"
            title="Run OF's own search across ~50 selling keywords and fold into local search"
          >
            {harvest ? `Harvesting ${harvest}` : "Harvest OF (50)"}
          </button>
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
      <div className="flex items-stretch rounded-xl border border-border overflow-hidden h-[74vh]">
        {/* LEFT rail — OF folders (independently scrollable) */}
        <aside className="w-56 shrink-0 border-r border-border bg-bg-elev-1/40 overflow-y-auto">
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
                  "w-6 h-6 grid place-items-center rounded-md hover:bg-bg-elev-1",
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
                  <div className="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity shrink-0">
                    <button
                      type="button"
                      onClick={(e) => {
                        e.stopPropagation();
                        onRenameFolder(f);
                      }}
                      title="Rename"
                      className="w-6 h-6 grid place-items-center rounded-md hover:bg-bg text-fg-dim hover:text-fg text-xs"
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
                      className="w-6 h-6 grid place-items-center rounded-md hover:bg-red-500/20 text-fg-dim hover:text-red-400 text-xs"
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
            <div className="flex items-center gap-2">
              <input
                value={searchRaw}
                onChange={(e) => setSearchRaw(e.target.value)}
                placeholder={useLocal ? "Search tags (instant)…" : "Search…"}
                className="px-3 py-1.5 rounded-lg text-sm bg-bg-elev-1 border border-border focus:border-accent outline-none w-48"
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

          <div ref={scrollRef} className="p-3 flex-1 min-h-0 overflow-y-auto">
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
                <div className="text-center text-xs text-fg-dim pt-1">
                  {src.isFetching ? "Loading…" : `${items.length} shown${src.hasMore ? " · scroll for more" : ""}`}
                </div>
              </>
            )}
          </div>
        </section>
      </div>

      {preview && !selectMode && (
        <div className="rounded-lg border border-border bg-bg-elev-1/40 p-3 space-y-2">
          <div className="flex items-center justify-between">
            <div className="text-sm font-medium">
              #{preview.id} · {preview.type}
              {preview.duration ? ` · ${Math.round(preview.duration)}s` : ""}
            </div>
            <button
              type="button"
              onClick={() => describeOne(preview)}
              disabled={!!busy}
              className="px-3 py-1 rounded-md text-xs border border-emerald-500/50 bg-emerald-500/10 text-emerald-400 hover:bg-emerald-500/20 disabled:opacity-50"
            >
              {busy === "describing"
                ? "Describing…"
                : (preview._ai as { describe_status?: string } | undefined)?.describe_status === "described"
                ? "Re-describe"
                : "Describe"}
            </button>
          </div>
          <AiDetail ai={preview._ai as Record<string, unknown> | undefined} />
        </div>
      )}
    </div>
  );
}

function AiDetail({ ai }: { ai?: Record<string, unknown> }) {
  if (!ai || (!ai.description && !ai.video_description && !(ai.tags as unknown[])?.length)) {
    return <div className="text-xs text-fg-dim">Not described yet — click Describe.</div>;
  }
  const desc = (ai.description || ai.video_description || "") as string;
  const tags = (ai.tags as string[] | undefined) ?? [];
  const tier = ai.explicitness_tier as string | undefined;
  const cap = ai.suggested_caption as string | undefined;
  const price = ai.suggested_price_cents as number | undefined;
  return (
    <div className="space-y-1.5 text-sm">
      {desc && <div className="text-fg">{desc}</div>}
      {tags.length > 0 && (
        <div className="flex flex-wrap gap-1">
          {tags.map((t) => (
            <span key={t} className="px-1.5 py-0.5 rounded bg-bg-elev-1 border border-border text-[11px] text-fg-dim">
              {t}
            </span>
          ))}
        </div>
      )}
      <div className="flex gap-3 text-[11px] text-fg-dim">
        {tier && <span>tier: {tier}</span>}
        {cap && <span>caption: “{cap}”</span>}
        {typeof price === "number" && <span>${(price / 100).toFixed(2)}</span>}
      </div>
    </div>
  );
}
