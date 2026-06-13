"use client";

/**
 * GifPicker — inline strip rendered above the composer toolbar (mirrors OF
 * desktop). The Composer owns the open/close state and the 🎬 toggle
 * button; this file exports the strip + a `toPicked` adapter.
 *
 * Wire-format: send via top-level `giphyId` on /chats/{id}/messages.
 * Verified live 2026-05-23.
 */

import { useEffect, useMemo, useRef, useState } from "react";

import { cn } from "@/lib/utils";
import {
  gifSearch, gifTrending,
  type GiphyItem, type GiphyResponse,
} from "@/lib/relay";

export interface PickedGif {
  id: string;
  /** Animated preview URL the chip + grid render. */
  thumbUrl: string;
  /** Full-size URL — useful for the optimistic message bubble later. */
  fullUrl: string;
  width?: number;
  height?: number;
  title?: string;
}

function toPicked(g: GiphyItem): PickedGif {
  const fh = g.images.fixed_height ?? g.images.original;
  const orig = g.images.original;
  return {
    id: g.id,
    thumbUrl: fh?.url ?? orig?.url ?? "",
    fullUrl: orig?.url ?? fh?.url ?? "",
    width: orig?.width ? Number(orig.width) : undefined,
    height: orig?.height ? Number(orig.height) : undefined,
    title: g.title,
  };
}

const PAGE_SIZE = 24;

/**
 * Inline strip — rendered above the composer textarea when the user
 * clicks 🎬. Search input runs the row across the top; below is a
 * horizontally-scrolling row of trending / search results.
 */
export function GifPickerStrip({
  accountId, onPick, onClose,
}: {
  accountId: string | null;
  onPick: (gif: PickedGif) => void;
  onClose: () => void;
}) {
  const [search, setSearch] = useState("");
  const [debouncedSearch, setDebouncedSearch] = useState("");
  const [items, setItems] = useState<GiphyItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const searchInputRef = useRef<HTMLInputElement | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  // Debounce the search box so we don't fire a request per keystroke.
  useEffect(() => {
    const id = setTimeout(() => setDebouncedSearch(search.trim()), 250);
    return () => clearTimeout(id);
  }, [search]);

  useEffect(() => {
    setTimeout(() => searchInputRef.current?.focus(), 50);
    return () => abortRef.current?.abort();
  }, []);

  // Fetch on mount + whenever the (debounced) query changes. AbortController
  // cancels an in-flight request when the query changes before it returns,
  // so a fast typer never sees stale results paint over fresh ones.
  useEffect(() => {
    abortRef.current?.abort();
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    setLoading(true);
    setError(null);
    const ctx = { accountId };
    const p: Promise<GiphyResponse> = debouncedSearch
      ? gifSearch(debouncedSearch, PAGE_SIZE, 0, ctx, ctrl.signal)
      : gifTrending(PAGE_SIZE, 0, ctx, ctrl.signal);
    p.then((resp) => {
      if (ctrl.signal.aborted) return;
      setItems(resp.data || []);
      const offset = resp.pagination?.offset ?? 0;
      const total = resp.pagination?.total_count ?? 0;
      setHasMore((resp.data?.length ?? 0) >= PAGE_SIZE && offset + PAGE_SIZE < total);
    }).catch((e: unknown) => {
      if (ctrl.signal.aborted) return;
      setError(e instanceof Error ? e.message : String(e));
      setItems([]);
      setHasMore(false);
    }).finally(() => {
      if (!ctrl.signal.aborted) setLoading(false);
    });
    return () => ctrl.abort();
  }, [debouncedSearch, accountId]);

  // Route loadMore through the same abortRef the main fetch effect uses, so a
  // query change (which aborts abortRef.current) also cancels an in-flight
  // loadMore. Otherwise the stale page resolves and appends the OLD query's
  // results onto the NEW query's first page.
  async function loadMore() {
    abortRef.current?.abort();
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    const ctx = { accountId };
    setLoading(true);
    try {
      const offset = items.length;
      const resp: GiphyResponse = debouncedSearch
        ? await gifSearch(debouncedSearch, PAGE_SIZE, offset, ctx, ctrl.signal)
        : await gifTrending(PAGE_SIZE, offset, ctx, ctrl.signal);
      if (ctrl.signal.aborted) return;
      setItems((prev) => [...prev, ...(resp.data || [])]);
      const total = resp.pagination?.total_count ?? 0;
      setHasMore((resp.data?.length ?? 0) >= PAGE_SIZE && offset + PAGE_SIZE < total);
    } catch (e) {
      if (ctrl.signal.aborted) return;
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      if (!ctrl.signal.aborted) setLoading(false);
    }
  }

  const grid = useMemo(() => items.map(toPicked), [items]);

  return (
    <div className="bg-bg/60 border border-border rounded-lg">
      <div className="p-2 border-b border-border flex items-center gap-2">
        <button
          type="button"
          onClick={onClose}
          className="w-6 h-6 grid place-items-center rounded-md text-fg-dim hover:text-fg hover:bg-bg-elev-1 shrink-0"
          aria-label="Close GIF picker"
          title="Close"
        >
          ✕
        </button>
        <input
          ref={searchInputRef}
          type="text"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Search GIFs…"
          className="flex-1 bg-transparent border border-border rounded-md px-2 py-1 text-xs placeholder:text-muted focus:outline-none focus:border-accent"
        />
        <span className="text-[10px] text-fg-dim shrink-0">
          {debouncedSearch ? "search" : "trending"}
        </span>
      </div>

      <div className="p-2">
        {error && (
          <div className="text-[11px] text-err py-2 text-center">
            {error.slice(0, 200)}
          </div>
        )}
        {grid.length === 0 && !loading && !error && (
          <div className="text-xs text-fg-dim text-center py-4">
            {debouncedSearch ? `No GIFs match "${debouncedSearch}".` : "No GIFs."}
          </div>
        )}
        {grid.length > 0 && (
          <div className="flex gap-1.5 overflow-x-auto pb-1 scrollbar-thin">
            {grid.map((g) => (
              <button
                key={g.id}
                type="button"
                onClick={() => onPick(g)}
                className={cn(
                  "shrink-0 w-32 h-32 rounded-md overflow-hidden border border-border bg-bg-elev-1",
                  "hover:border-accent focus:border-accent focus:outline-none",
                )}
                title={g.title}
              >
                {g.thumbUrl ? (
                  // eslint-disable-next-line @next/next/no-img-element
                  <img
                    src={g.thumbUrl}
                    alt={g.title || "GIF"}
                    loading="lazy"
                    className="w-full h-full object-cover"
                  />
                ) : null}
              </button>
            ))}
            {hasMore && (
              <button
                type="button"
                onClick={loadMore}
                disabled={loading}
                className="shrink-0 w-24 h-32 rounded-md border border-dashed border-border text-[11px] text-fg-dim hover:text-fg hover:bg-bg-elev-1 disabled:opacity-40"
              >
                {loading ? "…" : "more →"}
              </button>
            )}
          </div>
        )}
        {loading && grid.length === 0 && (
          <div className="text-[11px] text-fg-dim text-center py-3">Loading…</div>
        )}
      </div>
    </div>
  );
}

/**
 * Toolbar toggle. Stateless — parent passes `open` + `onToggle`; the
 * actual GIF strip mounts elsewhere in the composer's layout.
 */
export function GifPickerButton({
  open, onToggle, disabled,
}: {
  open: boolean;
  onToggle: () => void;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={onToggle}
      className={cn(
        "w-8 h-8 grid place-items-center rounded-md border text-sm",
        open
          ? "bg-accent/15 text-accent border-accent/40"
          : "bg-transparent text-fg-dim border-border hover:bg-bg-elev-1",
        disabled && "opacity-40 cursor-not-allowed",
      )}
      aria-label="GIF picker"
      title="Send a GIF"
    >
      🎬
    </button>
  );
}
