"use client";

/**
 * VaultFolderPicker — a fan-agnostic, multi-select VAULT FOLDER picker for
 * config screens (Tip Reward tiers, and any other "type a folder name" field).
 *
 * Unlike chat/VaultPicker (which picks individual MEDIA for one fan and is
 * coupled to a fanId / per-fan MRU / localStorage state), this picks whole
 * FOLDERS with no fan in scope, and returns folder NAMES — the shape the
 * tip_reward automation already stores and resolves. It reuses VaultPicker's
 * exported video helpers (`videoPosterFrames`, `isDrmOnlyVideo`) so videos —
 * including DRM-only ones, which can't preview/play but ARE sendable — show a
 * poster and are never excluded.
 *
 * UX: a modal listing the account's custom folders (useVaultLists). Each row
 * has a checkbox (multi-select) + name + a photo/video/gif count badge, and
 * expands to a media grid (useVaultMedia, all/photo/video/gif chips) so the
 * creator SEES the images and videos inside before picking. Folder names that
 * are stored in the config but no longer exist in the vault are preserved and
 * shown as removable chips, so saving never silently drops an enabled tier.
 */

import { useEffect, useMemo, useState } from "react";
import { createPortal } from "react-dom";
import { Library, ChevronRight, Check, Film, Image as ImageIcon } from "lucide-react";

import { Button } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";
import { useVaultLists, useVaultMedia } from "@/hooks/useVaultMedia";
import { proxyImage, type VaultList, type VaultMedia } from "@/lib/relay";
import { videoPosterFrames, isDrmOnlyVideo } from "@/components/chat/VaultPicker";

type MediaType = "all" | "photo" | "video" | "gif";

const TYPE_CHIPS: Array<{ value: MediaType; label: string }> = [
  { value: "all", label: "All" },
  { value: "photo", label: "Photos" },
  { value: "video", label: "Videos" },
  { value: "gif", label: "GIFs" },
];

export interface VaultFolderPickerProps {
  open: boolean;
  onClose: () => void;
  accountId: string | null;
  /** Folder NAMES already chosen (matched to live folders case-insensitively). */
  initialSelected: string[];
  /** Returns the chosen folder NAMES (preserves any not in the live vault). */
  onConfirm: (folderNames: string[]) => void;
}

const sameName = (a: string, b: string) =>
  a.trim().toLowerCase() === b.trim().toLowerCase();

export function VaultFolderPicker({
  open, onClose, accountId, initialSelected, onConfirm,
}: VaultFolderPickerProps) {
  // Selected folder NAMES (canonical-cased once matched to a live folder). Kept
  // as an array so order is stable and stale-but-stored names survive a save.
  const [selected, setSelected] = useState<string[]>([]);
  const [expanded, setExpanded] = useState<number | null>(null);

  const listsQ = useVaultLists(accountId, open);
  const folders = useMemo<VaultList[]>(() => listsQ.data?.list ?? [], [listsQ.data]);

  // Seed from the incoming config every time the modal opens.
  useEffect(() => {
    if (open) {
      setSelected(initialSelected.filter((s) => s.trim()));
      setExpanded(null);
    }
  }, [open]); // eslint-disable-line react-hooks/exhaustive-deps

  if (!open) return null;

  const isSel = (name: string) => selected.some((s) => sameName(s, name));
  const toggle = (name: string) =>
    setSelected((prev) =>
      prev.some((s) => sameName(s, name))
        ? prev.filter((s) => !sameName(s, name))
        : [...prev, name],
    );

  // Stored names with no matching live folder — preserved, shown for removal.
  const orphanNames = selected.filter(
    (s) => !folders.some((f) => sameName(f.name, s)),
  );

  const modal = (
    <div
      className="fixed inset-0 z-[100] flex items-center justify-center bg-black/60 p-0 sm:p-4"
      onClick={onClose}
    >
      <div
        className="bg-panel border-0 sm:border border-border rounded-none sm:rounded-xl w-full h-full sm:h-auto max-w-none sm:max-w-2xl max-h-none sm:max-h-[85vh] flex flex-col shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center gap-2 px-4 py-3 border-b border-border">
          <Library className="size-4 text-accent" />
          <h3 className="text-sm font-medium">Pick vault folders</h3>
          <span className="text-xs text-fg-dim">
            {selected.length} selected
          </span>
          <button
            onClick={onClose}
            className="ml-auto text-fg-dim hover:text-fg text-lg leading-none px-1 size-10 sm:size-auto grid place-items-center sm:block"
            title="Close"
            aria-label="Close"
          >
            ×
          </button>
        </div>

        {/* Body */}
        <div className="flex-1 overflow-y-auto p-3 space-y-2">
          {listsQ.isLoading ? (
            <div className="text-sm text-fg-dim p-3">Loading folders…</div>
          ) : folders.length === 0 ? (
            <div className="text-sm text-fg-dim p-3">
              No custom vault folders with media. Create a folder on OnlyFans and
              add some content to it first.
            </div>
          ) : (
            folders.map((f) => (
              <FolderRow
                key={f.id}
                folder={f}
                accountId={accountId}
                selected={isSel(f.name)}
                expanded={expanded === f.id}
                onToggle={() => toggle(f.name)}
                onExpand={() => setExpanded((cur) => (cur === f.id ? null : f.id))}
              />
            ))
          )}

          {/* Stored folder names that aren't in the current vault (kept on save). */}
          {orphanNames.length > 0 && (
            <div className="pt-2 border-t border-border space-y-1.5">
              <div className="text-[11px] text-warn">
                Selected, but not found in the current vault (kept on save):
              </div>
              <div className="flex flex-wrap gap-1.5">
                {orphanNames.map((nm) => (
                  <span
                    key={nm}
                    className="inline-flex items-center gap-1 text-xs bg-bg-elev-1 border border-border rounded-full px-2 py-0.5"
                  >
                    {nm}
                    <button
                      type="button"
                      onClick={() => toggle(nm)}
                      className="text-fg-dim hover:text-err leading-none"
                      title="Remove"
                    >
                      ×
                    </button>
                  </span>
                ))}
              </div>
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="flex items-center gap-2 px-4 py-3 pb-[max(0.75rem,env(safe-area-inset-bottom))] sm:pb-3 border-t border-border">
          <Button onClick={() => { onConfirm(selected); onClose(); }}>
            Use {selected.length} folder{selected.length === 1 ? "" : "s"}
          </Button>
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
        </div>
      </div>
    </div>
  );

  return typeof document !== "undefined" ? createPortal(modal, document.body) : null;
}

// ── one folder row: checkbox + name + counts, expands to a media grid ──────

function FolderRow({
  folder, accountId, selected, expanded, onToggle, onExpand,
}: {
  folder: VaultList;
  accountId: string | null;
  selected: boolean;
  expanded: boolean;
  onToggle: () => void;
  onExpand: () => void;
}) {
  const counts: string[] = [];
  if (folder.photosCount) counts.push(`${folder.photosCount} photo${folder.photosCount === 1 ? "" : "s"}`);
  if (folder.videosCount) counts.push(`${folder.videosCount} video${folder.videosCount === 1 ? "" : "s"}`);
  if (folder.gifsCount) counts.push(`${folder.gifsCount} gif${folder.gifsCount === 1 ? "" : "s"}`);

  return (
    <div className={cn(
      "rounded-lg border bg-bg-elev-1 overflow-hidden",
      selected ? "border-accent" : "border-border",
    )}>
      <div className="flex items-center gap-2 p-2.5">
        <button
          type="button"
          onClick={onToggle}
          className={cn(
            "size-5 rounded border grid place-items-center shrink-0",
            selected ? "bg-accent border-accent text-white" : "border-border bg-bg",
          )}
          title={selected ? "Deselect folder" : "Select folder"}
        >
          {selected && <Check className="size-3.5" />}
        </button>
        <button
          type="button"
          onClick={onToggle}
          className="text-sm text-fg text-left truncate flex-1 min-w-0"
        >
          {folder.name}
          {counts.length > 0 && (
            <span className="text-[11px] text-fg-dim ml-2">{counts.join(" · ")}</span>
          )}
        </button>
        <button
          type="button"
          onClick={onExpand}
          className="flex items-center gap-1 text-xs text-fg-dim hover:text-accent px-1.5 py-1 shrink-0"
          title="Preview contents"
        >
          Preview
          <ChevronRight className={cn("size-3.5 transition-transform", expanded && "rotate-90")} />
        </button>
      </div>
      {expanded && <FolderPreview folder={folder} accountId={accountId} />}
    </div>
  );
}

// ── media grid for one folder (photos + videos, lazily fetched on expand) ──

function FolderPreview({ folder, accountId }: { folder: VaultList; accountId: string | null }) {
  const [type, setType] = useState<MediaType>("all");
  const vault = useVaultMedia({ accountId, type, listId: folder.id, enabled: true });

  return (
    <div className="border-t border-border p-2.5 space-y-2 bg-bg/40">
      <div className="flex flex-wrap gap-1">
        {TYPE_CHIPS.map((c) => (
          <button
            key={c.value}
            type="button"
            onClick={() => setType(c.value)}
            className={cn(
              "text-[11px] px-2 py-0.5 rounded-full border",
              type === c.value
                ? "bg-accent border-accent text-white"
                : "border-border text-fg-dim hover:border-accent",
            )}
          >
            {c.label}
          </button>
        ))}
      </div>

      {vault.isLoading ? (
        <div className="text-xs text-fg-dim py-2">Loading…</div>
      ) : vault.items.length === 0 ? (
        <div className="text-xs text-fg-dim py-2">Nothing here for this filter.</div>
      ) : (
        <>
          <div className="grid grid-cols-3 gap-1.5 sm:grid-cols-5">
            {vault.items.map((m) => (
              <MediaTile key={m.id} media={m} accountId={accountId} />
            ))}
          </div>
          {vault.hasMore && (
            <button
              type="button"
              onClick={() => vault.loadMore()}
              disabled={vault.isFetchingNextPage}
              className="text-xs text-accent hover:underline disabled:opacity-50"
            >
              {vault.isFetchingNextPage ? "Loading…" : "Load more"}
            </button>
          )}
        </>
      )}
    </div>
  );
}

/** A read-only preview tile. Photos use their thumb; videos use OF's poster
 *  frame (DRM-only videos can't play but DO have a poster). A ▶ badge marks
 *  videos; a DRM tag marks the unplayable-but-sendable ones. */
function MediaTile({ media, accountId }: { media: VaultMedia; accountId: string | null }) {
  const isVideo = media.type === "video";
  const poster = isVideo
    ? videoPosterFrames(media)[0] || media.files?.thumb?.url || media.files?.preview?.url
    : media.files?.squarePreview?.url || media.files?.thumb?.url || media.files?.preview?.url;
  const url = poster ? proxyImage(poster, accountId) : "";

  return (
    <div className="relative aspect-square rounded overflow-hidden border border-border bg-bg">
      {url ? (
        <img src={url} alt="" loading="lazy" className="w-full h-full object-cover" />
      ) : (
        <span className="w-full h-full grid place-items-center text-fg-dim">
          {isVideo ? <Film className="size-4" /> : <ImageIcon className="size-4" />}
        </span>
      )}
      {isVideo && (
        <span className="absolute bottom-0.5 right-0.5 bg-black/60 rounded px-1 text-[9px] text-white leading-tight">
          {isDrmOnlyVideo(media) ? "DRM" : "▶"}
        </span>
      )}
    </div>
  );
}
