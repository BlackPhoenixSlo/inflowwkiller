"use client";

/**
 * TemplatePicker — popover for picking a saved reply or the welcome
 * message template, then inserting it into the composer.
 *
 * Two sources merged into one list:
 *   • OF welcome message (badge: 👋) — only one per account.
 *   • Local saved replies — Fastt-side, since OF rejects template
 *     creates for everything else.
 *
 * Pick a row → its text fills the textarea + its media attaches to the
 * outgoing message.
 */

import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";

import { useSavedReplies } from "@/hooks/useSavedReplies";
import { useTemplates } from "@/hooks/useTemplates";
import { cn } from "@/lib/utils";
import type { OFMessageTemplate, SavedReply, VaultMedia } from "@/lib/relay";

/** Unified shape so Composer doesn't care where the row came from. */
export interface PickedTemplate {
  source: "of" | "local";
  text: string;       // plain text the textarea wants
  displayText: string; // possibly HTML for the picker preview
  mediaCount: number;
  price: number;
  lockedText: boolean;
  isWelcome: boolean;
  media: VaultMedia[];
  title?: string;
  /** Vault ids inside `media` that should ride along UNLOCKED when
   *  `price > 0` is applied to the composer. Local templates carry this
   *  directly; OF templates can't, so they default to empty. */
  previews: number[];
  /** OF user ids of creators to @-tag when this template is applied.
   *  Local-only — OF welcome templates don't carry userTags. */
  taggedUsers: number[];
  /** Optional Giphy id stored on the template (local only). On apply the
   *  composer seeds its picked-gifs state with this so the GIF is sent
   *  alongside the text. */
  gifId?: string | null;
  /** Animated preview URL to render the chip without a fresh Giphy fetch. */
  gifUrl?: string | null;
  /** Script grouping (local only). Templates that share a `scriptId` form
   *  a sequence ordered by `scriptStep` — picking one advances the chat's
   *  next-in-script bubble. */
  scriptId?: string | null;
  scriptStep?: number | null;
}

function fromOF(t: OFMessageTemplate): PickedTemplate {
  return {
    source: "of",
    text: stripHtml(t.displayText || t.text),
    displayText: t.displayText || t.text,
    mediaCount: t.mediaCount ?? (t.media?.length ?? 0),
    price: t.price ?? 0,
    lockedText: !!t.lockedText,
    isWelcome: t.template === "reply_on_subscribe",
    media: (t.media ?? []).map((m) => ({
      id: m.id,
      type: (m.type as VaultMedia["type"]) || "photo",
      files: m.files ?? null,
    })),
    previews: [],
    taggedUsers: [],
  };
}

export function fromLocal(r: SavedReply): PickedTemplate {
  return {
    source: "local",
    text: r.text,
    displayText: r.text,
    mediaCount: r.media?.length ?? 0,
    price: r.price ?? 0,
    lockedText: !!r.locked_text,
    isWelcome: false,
    title: r.title ?? undefined,
    media: (r.media ?? []).map((m) => ({
      id: m.id,
      type: (m.type as VaultMedia["type"]) || "photo",
      files: m.files ?? null,
    })),
    previews: r.previews ?? [],
    taggedUsers: r.tagged_users ?? [],
    gifId: r.gif_id ?? null,
    gifUrl: r.gif_url ?? null,
    scriptId: r.script_id ?? null,
    scriptStep: r.script_step ?? null,
  };
}

export interface TemplatePickerProps {
  accountId: string | null;
  onPick: (t: PickedTemplate) => void;
  /** Surfaces that can't currently send vault images (mass / post / model-to-
   *  models) pass this to hide templates whose media contains any photo/video/
   *  audio. GIF-only templates (no photo/video/audio media — GIFs live in the
   *  template's `gifId`, not in `media`) pass through. */
  hideImageTemplates?: boolean;
}

/** Returns true when the template carries no photo/video/audio attachment.
 *  Templates with only text or only a GIF reference pass the filter. */
function templateHasNoImageMedia(t: PickedTemplate): boolean {
  if (!t.media || t.media.length === 0) return true;
  return !t.media.some((m) => {
    const tp = (m.type || "").toLowerCase();
    // "gif" templates live in the standalone gifId/gifUrl fields when added;
    // any media entry with photo/video/audio (or unknown non-gif) counts as
    // an image-bearing template.
    return tp !== "gif";
  });
}

export function TemplatePicker({ accountId, onPick, hideImageTemplates }: TemplatePickerProps) {
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState("");
  const rootRef = useRef<HTMLDivElement | null>(null);
  const buttonRef = useRef<HTMLButtonElement | null>(null);
  const popRef = useRef<HTMLDivElement | null>(null);
  const searchRef = useRef<HTMLInputElement | null>(null);
  const [coords, setCoords] = useState<{
    top?: number;
    bottom?: number;
    left: number;
    maxHeight: number;
  } | null>(null);
  const tplQ = useTemplates(open ? accountId : null);
  const replyQ = useSavedReplies(open ? accountId : null);

  // Reset + focus search when opening so picker is keyboard-first.
  useEffect(() => {
    if (!open) {
      setSearch("");
      return;
    }
    const t = setTimeout(() => searchRef.current?.focus(), 30);
    return () => clearTimeout(t);
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onClick = (e: MouseEvent) => {
      const t = e.target as Node;
      if (rootRef.current?.contains(t)) return;
      if (popRef.current?.contains(t)) return;
      setOpen(false);
    };
    document.addEventListener("pointerdown", onClick);
    return () => document.removeEventListener("pointerdown", onClick);
  }, [open]);

  useLayoutEffect(() => {
    if (!open) return;
    const update = () => {
      const r = buttonRef.current?.getBoundingClientRect();
      if (!r) return;
      const PANEL_W = 320;
      const PANEL_H_PREF = 384;
      const margin = 8;
      const wantLeft = r.left;
      const maxLeft = window.innerWidth - PANEL_W - margin;
      const left = Math.max(margin, Math.min(wantLeft, maxLeft));
      const spaceAbove = r.top - margin;
      const spaceBelow = window.innerHeight - r.bottom - margin;
      const openDown = spaceBelow >= PANEL_H_PREF || spaceBelow > spaceAbove;
      setCoords(openDown
        ? { top: r.bottom + 8, left, maxHeight: Math.min(PANEL_H_PREF, spaceBelow) }
        : { bottom: window.innerHeight - r.top + 8, left, maxHeight: Math.min(PANEL_H_PREF, spaceAbove) });
    };
    update();
    window.addEventListener("resize", update);
    window.addEventListener("scroll", update, true);
    return () => {
      window.removeEventListener("resize", update);
      window.removeEventListener("scroll", update, true);
    };
  }, [open]);

  // Welcome first (it's most-often used as the high-leverage reply),
  // then saved replies — newest-edit first as the local API returns.
  const welcome = (tplQ.data ?? []).find((t) => t.template === "reply_on_subscribe");
  const ofRows: PickedTemplate[] = welcome ? [fromOF(welcome)] : [];
  const localRows: PickedTemplate[] = (replyQ.data ?? []).map(fromLocal);
  const allItems = [...ofRows, ...localRows];
  const visibleByImageFilter = hideImageTemplates
    ? allItems.filter(templateHasNoImageMedia)
    : allItems;
  const hiddenCount = hideImageTemplates ? allItems.length - visibleByImageFilter.length : 0;

  // Search across title, body text, and script id — case-insensitive.
  const items = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return visibleByImageFilter;
    return visibleByImageFilter.filter((t) => {
      if (t.title && t.title.toLowerCase().includes(q)) return true;
      if (t.text && t.text.toLowerCase().includes(q)) return true;
      if (t.scriptId && t.scriptId.toLowerCase().includes(q)) return true;
      return false;
    });
  }, [search, visibleByImageFilter]);

  const loading = tplQ.isFetching || replyQ.isFetching;
  const err = tplQ.error || replyQ.error;

  return (
    <div ref={rootRef} className="relative">
      <button
        ref={buttonRef}
        type="button"
        onClick={() => setOpen((v) => !v)}
        disabled={!accountId}
        title="Insert saved template"
        className={cn(
          "w-8 h-8 grid place-items-center rounded-md border text-sm",
          open
            ? "bg-accent/15 text-accent border-accent/40"
            : "bg-transparent text-fg-dim border-border hover:bg-bg-elev-1",
          !accountId && "opacity-40 cursor-not-allowed",
        )}
      >
        ⌘
      </button>
      {open && coords && typeof document !== "undefined" && createPortal(
        <div
          ref={popRef}
          style={{
            position: "fixed",
            top: coords.top,
            bottom: coords.bottom,
            left: coords.left,
            maxHeight: coords.maxHeight,
          }}
          className="w-80 flex flex-col overflow-hidden bg-panel border border-border rounded-lg shadow-xl z-[100]"
        >
          <div className="px-3 py-2 border-b border-border space-y-1.5">
            <input
              ref={searchRef}
              type="text"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Escape") {
                  if (search) {
                    e.preventDefault();
                    setSearch("");
                  } else {
                    setOpen(false);
                  }
                }
                if (e.key === "Enter" && items.length > 0) {
                  e.preventDefault();
                  onPick(items[0]);
                  setOpen(false);
                }
              }}
              placeholder="Search templates…"
              className="w-full bg-bg border border-border rounded-md px-2 py-1 text-base md:text-sm focus:outline-none focus:border-accent"
            />
            <div className="text-[10px] text-fg-dim">
              {loading
                ? "Loading…"
                : `${items.length} of ${visibleByImageFilter.length}`}
              {search && items.length > 0 && " · Enter to pick first"}
            </div>
          </div>
          <div className="flex-1 overflow-y-auto">
            {err && (
              <div className="px-3 py-2 text-xs text-err">
                {(err as Error).message || "failed"}
              </div>
            )}
            {!loading && items.length === 0 && (
              <div className="px-3 py-4 text-xs text-fg-dim text-center">
                {search
                  ? `No templates match "${search}".`
                  : hideImageTemplates && allItems.length > 0
                    ? `No text/GIF-only templates. ${allItems.length} hidden because they contain images.`
                    : "No templates yet. Create one in Settings → Templates."}
              </div>
            )}
            {hiddenCount > 0 && items.length > 0 && !search && (
              <div className="px-3 py-1.5 text-[10px] text-fg-dim border-b border-border/40 bg-bg/40">
                {hiddenCount} template{hiddenCount === 1 ? "" : "s"} hidden — contain images, not supported here.
              </div>
            )}
            {items.map((t, i) => (
              <button
                key={`${t.source}:${t.title ?? i}:${i}`}
                type="button"
                onClick={() => { onPick(t); setOpen(false); }}
                className="w-full text-left px-3 py-2 border-b border-border/40 hover:bg-bg-elev-1 transition-colors"
              >
                <div className="flex items-center flex-wrap md:flex-nowrap gap-x-2 gap-y-0.5 mb-0.5">
                  {t.isWelcome && (
                    <span className="text-[10px] px-1.5 py-0.5 rounded bg-accent/15 text-accent">👋 welcome</span>
                  )}
                  {t.source === "local" && (
                    <span className="text-[10px] px-1.5 py-0.5 rounded bg-bg-elev-1 text-fg-dim">local</span>
                  )}
                  {t.title && (
                    <span className="text-[10px] font-medium text-fg">{t.title}</span>
                  )}
                  {t.mediaCount > 0 && (
                    <span className="text-[10px] text-fg-dim">📎 {t.mediaCount}</span>
                  )}
                  {t.gifId && (
                    <span className="text-[10px] text-fg-dim" title="Includes a GIF">
                      🎬<span className="md:hidden"> gif</span>
                    </span>
                  )}
                  {t.scriptId && (
                    <span
                      className="text-[10px] px-1 rounded bg-accent/10 text-accent"
                      title={`Part of script "${t.scriptId}" · step ${t.scriptStep ?? "?"}`}
                    >
                      ▶ {t.scriptId}{t.scriptStep != null ? `·${t.scriptStep}` : ""}
                    </span>
                  )}
                  {t.price > 0 && (
                    <span className="text-[10px] text-warn">🔒 ${t.price.toFixed(2)}</span>
                  )}
                  {t.previews.length > 0 && (
                    <span className="text-[10px] text-fg-dim" title="Free previews on apply">
                      👁 {t.previews.length}<span className="md:hidden"> free</span>
                    </span>
                  )}
                  {t.taggedUsers.length > 0 && (
                    <span className="text-[10px] text-accent" title="@-tags applied on pick">
                      @{t.taggedUsers.length}<span className="md:hidden"> tagged</span>
                    </span>
                  )}
                </div>
                <div className="text-sm md:text-xs text-fg line-clamp-2">{t.text}</div>
              </button>
            ))}
          </div>
        </div>,
        document.body,
      )}
    </div>
  );
}

/** Back-compat: composer calls this with the picked item's media. */
export function templateMediaToVault(t: PickedTemplate): VaultMedia[] {
  return t.media;
}

function stripHtml(s: string): string {
  return s
    .replace(/<br\s*\/?>/gi, "\n")
    .replace(/<\/p\s*>/gi, "\n")
    .replace(/<[^>]+>/g, "")
    .replace(/&nbsp;/g, " ")
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, "\"")
    .replace(/&#39;/g, "'")
    .trim();
}
