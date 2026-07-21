"use client";

/**
 * PinnedPanel — the in-chat pinned-messages surface.
 *
 *   PinnedBar      — a thin row that sits between the chat header and the
 *                    message list. Renders only when ≥1 message in the
 *                    loaded backlog has `isPinned: true`. Click toggles
 *                    the popover.
 *
 *   PinnedPopover  — a floating panel anchored bottom-left of the message
 *                    scroll area. Scrollable list of pinned messages with
 *                    text preview, sender name, time, and an optional
 *                    "📎 N" media badge. Images are NOT loaded — clicking
 *                    a row jumps to the bubble in the main thread where
 *                    MediaTile owns the actual fetch.
 *
 * Hide rules for the popover:
 *   • Click anywhere outside the popover AND outside the composer area
 *     (textarea, emoji bar, action buttons) → close.
 *   • Composer click → keep open (the chatter is likely about to type
 *     based on the pinned context).
 *   • Send fires → parent closes by calling `onClose()`.
 *
 * Pinned messages are sourced from the same `["messages", aid, fanId]`
 * cache that drives the main thread — no extra OF fetch. If you've
 * scrolled up to load older pages, those pinned items will appear too;
 * otherwise the popover shows only the pinned items inside the loaded
 * window (the last ~30 messages by default).
 */

import { useEffect, useMemo, useRef, useState } from "react";

import { cn, decodeHtmlEntities } from "@/lib/utils";
import type { OFMessage } from "@/lib/relay";

// Width-match the ChatList sidebar (320px in app/app/inbox/page.tsx) so
// the popover snaps to the same column it overlays.
const CHAT_LIST_WIDTH = 320;
// Fallback top offset (px) if no `[data-chat-row]` elements are found
// — covers the case where the chat list hasn't rendered yet or the
// inbox is empty. Roughly: nav (56) + filter chips (~120) + 3 rows (~210).
const FALLBACK_TOP_PX = 386;

interface PinnedBarProps {
  pinned: OFMessage[];
  open: boolean;
  onToggle: () => void;
}

export function PinnedBar({ pinned, open, onToggle }: PinnedBarProps) {
  if (pinned.length === 0) return null;
  return (
    <button
      type="button"
      onClick={onToggle}
      className={cn(
        "border-b border-border px-4 py-1.5 flex items-center gap-2 text-[11px]",
        "bg-panel/60 hover:bg-bg-elev-1/60 transition-colors text-left",
        open && "bg-bg-elev-1/80",
      )}
      title={open ? "Close pinned messages" : "Show pinned messages"}
    >
      <span aria-hidden>📌</span>
      <span className="text-fg">
        {pinned.length} pinned message{pinned.length === 1 ? "" : "s"}
      </span>
      <span className="text-fg-dim">
        · click to {open ? "close" : "view"}
      </span>
    </button>
  );
}

interface PinnedPopoverProps {
  pinned: OFMessage[];
  ownerUserId: number | null;
  open: boolean;
  onClose: () => void;
  onJumpTo: (messageId: number) => void;
}

export function PinnedPopover({
  pinned, ownerUserId, open, onClose, onJumpTo,
}: PinnedPopoverProps) {
  const ref = useRef<HTMLDivElement | null>(null);
  // Top edge in viewport pixels. Measured from the 3rd ChatList row's
  // bottom so the popover overlays the rest of the chat list while
  // keeping the top three conversations visible above it.
  const [topPx, setTopPx] = useState<number>(FALLBACK_TOP_PX);

  // Newest pinned first — chatters usually pin "the latest thing we
  // agreed on" so the most recent pin is the most actionable.
  const ordered = useMemo(() => {
    return [...pinned].sort((a, b) => {
      const ta = a.createdAt ? Date.parse(a.createdAt) || 0 : 0;
      const tb = b.createdAt ? Date.parse(b.createdAt) || 0 : 0;
      return tb - ta;
    });
  }, [pinned]);

  // Click-out handler. Composer carries `data-composer-area` so the
  // chatter can keep typing in the input / press emoji / vault buttons
  // without dismissing the panel. Anything else closes it. ChatList
  // rows above the popover (the top 3) are exempt too so the chatter
  // can still switch conversations without dismissing the panel — the
  // panel content travels with the new chat anyway via the messages
  // cache.
  useEffect(() => {
    if (!open) return;
    const onDocClick = (e: MouseEvent) => {
      const target = e.target as Node | null;
      if (!target) return;
      if (ref.current?.contains(target)) return;
      const el = target as HTMLElement;
      if (el.closest && el.closest("[data-composer-area]")) return;
      if (el.closest && el.closest("[data-chat-row]")) return;
      onClose();
    };
    const t = setTimeout(() => {
      document.addEventListener("pointerdown", onDocClick);
    }, 0);
    return () => {
      clearTimeout(t);
      document.removeEventListener("pointerdown", onDocClick);
    };
  }, [open, onClose]);

  // Measure the 3rd ChatList row's bottom so the popover snaps just
  // below it. Re-measure on open + on window resize so the layout
  // stays correct if the chatter resizes their window.
  useEffect(() => {
    if (!open) return;
    const measure = () => {
      const rows = document.querySelectorAll<HTMLElement>("[data-chat-row]");
      // Use the third row if present, otherwise the last row we found,
      // otherwise the fallback. The "max" interpretation per the user
      // spec: keep at LEAST 3 rows visible; if there are fewer, show
      // them all and start the popover below whatever's there.
      const anchor = rows[2] ?? rows[rows.length - 1];
      if (!anchor) {
        setTopPx(FALLBACK_TOP_PX);
        return;
      }
      const rect = anchor.getBoundingClientRect();
      setTopPx(Math.round(rect.bottom));
    };
    measure();
    // Re-measure when rows mount in later (chat list lazily renders);
    // a short tick after open catches the common case where the list
    // is still loading.
    const t = setTimeout(measure, 80);
    window.addEventListener("resize", measure);
    return () => {
      clearTimeout(t);
      window.removeEventListener("resize", measure);
    };
  }, [open]);

  if (!open || ordered.length === 0) return null;

  return (
    <div
      ref={ref}
      style={{ top: topPx, left: 0, width: CHAT_LIST_WIDTH }}
      className={cn(
        "fixed bottom-0 z-30",
        "bg-panel border-t border-r border-border shadow-2xl",
        "flex flex-col overflow-hidden",
      )}
      role="dialog"
      aria-label="Pinned messages"
    >
      <div className="px-3 py-2 border-b border-border flex items-center gap-2 text-[12px]">
        <span aria-hidden>📌</span>
        <span className="font-medium">Pinned messages</span>
        <span className="text-fg-dim">· {ordered.length}</span>
        <button
          type="button"
          onClick={onClose}
          className="ml-auto text-fg-dim hover:text-fg leading-none"
          aria-label="Close"
          title="Close"
        >
          ✕
        </button>
      </div>
      <div className="overflow-y-auto flex-1">
        {ordered.map((m) => {
          const isOutgoing = ownerUserId != null && m.fromUser?.id === ownerUserId;
          const senderName = isOutgoing
            ? "You"
            : decodeHtmlEntities(m.fromUser?.name || m.fromUser?.username || "fan");
          const preview = stripPreview(m.text || "", 120);
          const mediaCount = m.media?.length ?? m.mediaCount ?? 0;
          return (
            <button
              key={String(m.id)}
              type="button"
              onClick={() => {
                onJumpTo(Number(m.id));
                onClose();
              }}
              className={cn(
                "w-full text-left px-3 py-2 border-b border-border/60 last:border-b-0",
                "hover:bg-bg-elev-1/40 transition-colors",
              )}
            >
              <div className="flex items-baseline gap-2 text-[11px]">
                <span className={cn("font-medium", isOutgoing ? "text-accent" : "text-fg")}>
                  {senderName}
                </span>
                <span className="text-fg-dim">{fmtTime(m.createdAt)}</span>
              </div>
              {preview && (
                <div className="text-[12px] text-fg-dim mt-0.5 break-words whitespace-pre-wrap line-clamp-3">
                  {preview}
                </div>
              )}
              {mediaCount > 0 && (
                <div className="text-[10px] text-fg-dim mt-1 inline-flex items-center gap-1">
                  <span aria-hidden>📎</span>
                  <span>{mediaCount} media — click to view</span>
                </div>
              )}
            </button>
          );
        })}
      </div>
    </div>
  );
}

/**
 * PinnedSidePanel — full-height, in-flow left column for the popout
 * window. Same data as the popover, different layout: permanent, no
 * click-out behavior, no z-index dance. Mirrors FanDrawer-when-pinned
 * on the right edge — both turn into a third column inside the chat
 * surface flex row.
 */
interface PinnedSidePanelProps {
  pinned: OFMessage[];
  ownerUserId: number | null;
  onJumpTo: (messageId: number) => void;
}

export function PinnedSidePanel({
  pinned, ownerUserId, onJumpTo,
}: PinnedSidePanelProps) {
  const ordered = useMemo(() => {
    return [...pinned].sort((a, b) => {
      const ta = a.createdAt ? Date.parse(a.createdAt) || 0 : 0;
      const tb = b.createdAt ? Date.parse(b.createdAt) || 0 : 0;
      return tb - ta;
    });
  }, [pinned]);

  return (
    <div
      className={cn(
        "hidden md:flex w-[280px] shrink-0 border-r border-border bg-panel",
        "flex-col h-full min-h-0",
      )}
      role="complementary"
      aria-label="Pinned messages"
    >
      <div className="px-3 py-2 border-b border-border flex items-center gap-2 text-[12px]">
        <span aria-hidden>📌</span>
        <span className="font-medium">Pinned</span>
        <span className="text-fg-dim">· {ordered.length}</span>
      </div>
      {ordered.length === 0 ? (
        <div className="px-3 py-6 text-[11px] text-fg-dim">
          Nothing pinned yet. Hover any message in the thread and hit
          <span className="mx-1 px-1 rounded bg-bg-elev-1 text-fg">📌 pin</span>
          to add it here.
        </div>
      ) : (
        <div className="overflow-y-auto flex-1">
          {ordered.map((m) => {
            const isOutgoing = ownerUserId != null && m.fromUser?.id === ownerUserId;
            const senderName = isOutgoing
              ? "You"
              : decodeHtmlEntities(m.fromUser?.name || m.fromUser?.username || "fan");
            const preview = stripPreview(m.text || "", 140);
            const mediaCount = m.media?.length ?? m.mediaCount ?? 0;
            return (
              <button
                key={String(m.id)}
                type="button"
                onClick={() => onJumpTo(Number(m.id))}
                className={cn(
                  "w-full text-left px-3 py-2 border-b border-border/60 last:border-b-0",
                  "hover:bg-bg-elev-1/40 transition-colors",
                )}
              >
                <div className="flex items-baseline gap-2 text-[11px]">
                  <span className={cn("font-medium", isOutgoing ? "text-accent" : "text-fg")}>
                    {senderName}
                  </span>
                  <span className="text-fg-dim">{fmtTime(m.createdAt)}</span>
                </div>
                {preview && (
                  <div className="text-[12px] text-fg-dim mt-0.5 break-words whitespace-pre-wrap line-clamp-4">
                    {preview}
                  </div>
                )}
                {mediaCount > 0 && (
                  <div className="text-[10px] text-fg-dim mt-1 inline-flex items-center gap-1">
                    <span aria-hidden>📎</span>
                    <span>{mediaCount} media — click to view</span>
                  </div>
                )}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}

function stripPreview(html: string, max: number): string {
  const plain = html.replace(/<[^>]+>/g, "").replace(/\s+/g, " ").trim();
  if (plain.length <= max) return plain;
  return plain.slice(0, max - 1) + "…";
}

// Absolute time, like the main thread bubbles (MessageList.fmtTime). Relative
// labels ("5m ago") were computed once at render and went stale while the
// panel stayed open; an absolute stamp needs no re-render to stay correct.
// Same-day pins show the time; older pins also show a short date so a pin
// from last week isn't an undated bare time.
function fmtTime(iso?: string): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const time = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const now = new Date();
  const sameDay =
    d.getFullYear() === now.getFullYear() &&
    d.getMonth() === now.getMonth() &&
    d.getDate() === now.getDate();
  if (sameDay) return time;
  return `${d.toLocaleDateString([], { month: "short", day: "numeric" })} ${time}`;
}
