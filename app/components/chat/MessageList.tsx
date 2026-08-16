"use client";

/**
 * MessageList — the message-history pane.
 *
 * Renders the OFMessage[] for one chat. Owns:
 *   • Scroll-only-if-at-bottom (snapshot scroll position before merge,
 *     restore if user was at the bottom OR initial load).
 *   • Load-older button at the top.
 *   • Per-message visual states: pending (faded), failed (red border +
 *     Retry button).
 *
 * Direction inference: `fromUser.id === ownerUserId` → outgoing (right-
 * aligned, accent bubble). Anything else → incoming (left-aligned).
 *
 * Empty/loading states pulled into the same component so the parent
 * doesn't have to switch.
 */

import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";

import { describeLoadError, proxyImage, type OFMedia, type OFMessage } from "@/lib/relay";
import { cn } from "@/lib/utils";
import { blurImageClass, useBlurMode } from "@/hooks/useBlurMode";
import { useCompactMedia } from "@/hooks/useCompactMedia";
import { useTranslateMode } from "@/hooks/useTranslateMode";
import { useTranslations, type BubbleTranslation } from "@/hooks/useTranslations";
import { useFanVaultHistory } from "@/hooks/useFanVaultHistory";
import { MediaTile, pickEagerMediaIds } from "@/components/chat/MediaTile";
import { automationLabel } from "@/components/messages/MessageRowGeneric";
import type { AttributionEntry } from "@/hooks/useChatAttribution";
import { ImageDescCaption } from "@/components/chat/ImageDescCaption";


export interface MessageListProps {
  messages: OFMessage[];
  ownerUserId: number | null;  // the model account's OF user id
  accountId: string | null;    // for routing CDN urls through /img proxy
  /** Fan whose chat this is. Drives the per-tile BOUGHT/FREE/PAID badge
   *  overlay on sent message media (FanVaultHistory lookup is per-fan). */
  fanId?: number | null;
  isLoading: boolean;
  isError: boolean;
  error?: Error | null;
  hasOlder: boolean;
  loadingOlder: boolean;
  onLoadOlder: () => void;
  onRetry: (tempId: number) => void;
  /** Cancel a scheduled send by its server job id. Bubbles only render the
   *  cancel button when this is provided (and the row has a `_scheduleJobId`). */
  onCancelScheduled?: (jobId: number) => void;
  /** Double-click toggle for likes on incoming messages. Optional so
   *  read-only contexts (history search, etc.) can render the same list. */
  onToggleLike?: (msg: OFMessage) => void;
  /** Click "reply" on a bubble's hover toolbar. Parent stores the quoted
   *  context and renders it above the composer. */
  onQuoteReply?: (msg: OFMessage) => void;
  /** Click "📌 pin" / "📌 unpin" on a bubble's hover toolbar. Flips the
   *  pinned state on OF; parent owns the optimistic patch via the hook. */
  onTogglePin?: (msg: OFMessage) => void;
  /** Click "🗑 unsend" on a bubble's hover toolbar. Removes the message
   *  from this chat via OF's edit-window DELETE. Parent owns the
   *  optimistic-remove + "also unsend from everyone" follow-up prompt
   *  when the message turns out to be part of a mass send. Only shown
   *  on outbound bubbles — OF refuses unsend on incoming. */
  onUnsend?: (msg: OFMessage) => void;
  /** Click "🎁 Send reward" on a tip bubble's hover toolbar. Manually fires
   *  the tip_reward automation for this fan (free vault images), reusing the
   *  same send path the inbound-tip webhook uses. Only shown on incoming tip
   *  bubbles (`msg.isTip`). Parent owns the POST + success/error toast. */
  onSendReward?: (msg: OFMessage) => void;
  /** Highlight a single message id (used by the search results panel
   *  click-to-jump). The bubble gets a transient ring + scrolls into view. */
  highlightId?: number | string | null;
  /** The OF chat's `lastReadMessageId` — the highest message id the fan
   *  has marked read. Drives the ✓✓ seen indicator on outgoing bubbles. */
  lastReadByPeerId?: number | null;
  /** Outbound-bubble employee attribution. Keyed by message_id (string).
   *  Null entries OR `display_name: null` mean "no label" (deleted
   *  employee or pre-DB-branch row) — the meta row guards on truthy
   *  display_name only. */
  attribution?: Record<string, AttributionEntry> | null;
  /** Inbound photo/gif reads, keyed by message_id (string) — the same text the
   *  chat engines see inline as "[he sent: …]". Missing key = that one was never
   *  described (arrived before the feature, flag off, or the LLM cap was hit);
   *  the caption then offers a manual read. ImageDescCaption owns the describe
   *  mutation itself, so no callback needs threading through here. */
  imageDesc?: Record<string, string> | null;
  /** Display name of the currently-picked employee. Used as the
   *  optimistic fallback for outbound bubbles that don't have a real
   *  message_id yet (tempId < 0) — the chatter sees their own name
   *  instantly without waiting for the attribution refetch. */
  currentEmployeeName?: string | null;
}

const AT_BOTTOM_PX = 80;

export function MessageList(props: MessageListProps) {
  const {
    messages, ownerUserId, accountId, fanId, isLoading, isError, error,
    hasOlder, loadingOlder, onLoadOlder, onRetry, onCancelScheduled,
    onToggleLike, onQuoteReply, onTogglePin, onUnsend, onSendReward,
    highlightId, lastReadByPeerId,
    attribution, currentEmployeeName, imageDesc,
  } = props;

  // 🌐 translate-to-English mode: batch-fetch translations for every loaded
  // text (keyed by stripped text, so optimistic-id churn is free) and hand
  // each Bubble its entry. Off (default) costs nothing — empty deps, no fetch.
  const [translateOn] = useTranslateMode();
  const translatableTexts = useMemo(
    () => (translateOn ? messages.filter((m) => m.text).map((m) => stripHtml(m.text)) : []),
    [messages, translateOn],
  );
  const translations = useTranslations(translatableTexts, translateOn);

  const containerRef = useRef<HTMLDivElement | null>(null);
  const topSentinelRef = useRef<HTMLDivElement | null>(null);
  const wasAtBottomRef = useRef(true);
  const lastLenRef = useRef(0);
  // True until the first non-empty message render has been pinned to the
  // bottom. Without this, the IntersectionObserver below fires the
  // moment the top sentinel becomes visible during the initial render
  // (when scrollTop is still 0), which yanks the view back to old
  // messages instead of letting the user see the newest ones.
  const firstLoadPendingRef = useRef(true);
  // "↓ Latest" affordance: shown when the user is scrolled up and there's
  // visible history above the fold. Doubles as the visual cue that the
  // initial auto-pin window is active (small label flips while pending).
  const [showJumpLatest, setShowJumpLatest] = useState(false);
  const [autoPinActive, setAutoPinActive] = useState(true);
  // Snapshot pre-render scrollHeight + scrollTop so a load-older insert
  // (which pushes content down) can keep the user's eye anchored on the
  // same message instead of jumping back to the bottom.
  const preMergeScrollRef = useRef<{ height: number; top: number } | null>(null);
  // Set true immediately before every programmatic `scrollTop = scrollHeight`
  // write (the 60Hz pin poll + the media-load RAF repin). The `scroll` event
  // those writes emit is indistinguishable from a user scroll, so the scroll
  // release handler consumes this flag to ignore our own writes — letting
  // genuine user scrolls (scrollbar-thumb drag, gutter click, momentum fling)
  // release the auto-pin even though they emit no wheel/touchmove/keydown.
  const programmaticScrollRef = useRef(false);

  // Capture scroll position BEFORE the next render commit.
  useLayoutEffect(() => {
    const c = containerRef.current;
    if (!c) return;
    wasAtBottomRef.current =
      c.scrollHeight - c.scrollTop - c.clientHeight < AT_BOTTOM_PX;
    preMergeScrollRef.current = { height: c.scrollHeight, top: c.scrollTop };
  });

  // After render: decide between "stick to bottom" (initial / new message)
  // and "preserve anchor" (older page loaded at top).
  useEffect(() => {
    const c = containerRef.current;
    if (!c) return;
    const initial = lastLenRef.current === 0 && messages.length > 0;
    const grew = messages.length > lastLenRef.current && !initial;
    if (initial) {
      // Initial paint: hard-pin to the bottom now + next frame. A separate
      // ResizeObserver effect (below) keeps re-pinning while
      // firstLoadPendingRef stays true, so late-loading media that grow
      // the container after this point still land us at the bottom.
      const pinBottom = () => { c.scrollTop = c.scrollHeight; };
      pinBottom();
      requestAnimationFrame(pinBottom);
      // Hard backstop: release the auto-pin after 5s regardless of user
      // interaction, so a chat with media that never settles can't keep
      // yanking the view forever. 5s (was 2s) covers the common case of
      // 4-5 image thumbs loading sequentially on a cold tab — fix #11.
      const t = setTimeout(() => {
        firstLoadPendingRef.current = false;
        setAutoPinActive(false);
      }, 5000);
      lastLenRef.current = messages.length;
      return () => clearTimeout(t);
    }
    if (wasAtBottomRef.current) {
      c.scrollTop = c.scrollHeight;
    } else if (grew && preMergeScrollRef.current) {
      // Older page came in at the top — adjust scrollTop by the height
      // delta so the previously-visible message stays in view.
      const delta = c.scrollHeight - preMergeScrollRef.current.height;
      if (delta > 0) c.scrollTop = preMergeScrollRef.current.top + delta;
    }
    lastLenRef.current = messages.length;
  }, [messages]);

  // Late-arriving media (large images, video posters) grow the *content*
  // after the initial pin. A ResizeObserver on the container itself
  // doesn't fire (the container's borderBox stays constant — flex-1
  // inside a fixed-height column; only `scrollHeight` grows).
  //
  // Two-track re-pin for the first 5s of pendingRef:
  //   1. Per-frame poll (60Hz). Cheap, idempotent (scrollTop = scrollHeight
  //      is a no-op when already at bottom). Catches every layout shift
  //      including ones the load listener might race past — browser
  //      scroll anchoring, font swap, late-arriving DOM, mediastrip
  //      relayout. This was the bug behind fix #11's "bounce to middle".
  //   2. Capture-phase `load` listener on the container. <img>/<video>
  //      `load` doesn't bubble; capture phase lets us catch them all.
  //      Triggers an extra RAF pin so the visual snap happens the same
  //      frame the image paints, not at the next 16ms tick.
  const hasMessages = messages.length > 0;
  useEffect(() => {
    if (!hasMessages) return;
    const c = containerRef.current;
    if (!c) return;
    let rafId: number | null = null;
    const repin = () => {
      if (!firstLoadPendingRef.current) return;
      if (rafId != null) return;
      rafId = requestAnimationFrame(() => {
        rafId = null;
        if (firstLoadPendingRef.current) {
          programmaticScrollRef.current = true;
          c.scrollTop = c.scrollHeight;
        }
      });
    };
    // Image/video load events don't bubble — use capture-phase listener.
    const onMediaLoad = (e: Event) => {
      const t = e.target as HTMLElement | null;
      if (!t) return;
      if (t.tagName === "IMG" || t.tagName === "VIDEO") repin();
    };
    c.addEventListener("load", onMediaLoad, true);
    // 60Hz poll for the first 5s. Stops as soon as pendingRef flips false.
    const pollId = window.setInterval(() => {
      if (!firstLoadPendingRef.current) {
        window.clearInterval(pollId);
        return;
      }
      programmaticScrollRef.current = true;
      c.scrollTop = c.scrollHeight;
    }, 16);
    // `wheel` / `touchmove` / `keydown` are user-only signals that release
    // the auto-pin immediately. They don't cover scrollbar-thumb drag,
    // scrollbar-gutter clicks or momentum/inertial flings the browser
    // dispatches only as `scroll` — those are handled by the guarded scroll
    // path in onScroll below (which distinguishes our own pin writes from a
    // real user scroll via programmaticScrollRef).
    const release = (e: Event) => {
      if (!e.isTrusted) return;
      firstLoadPendingRef.current = false;
      setAutoPinActive(false);
    };
    c.addEventListener("wheel", release, { passive: true });
    c.addEventListener("touchmove", release, { passive: true });
    c.addEventListener("keydown", release);
    // "↓ Latest" affordance: passive scroll listener tracks distance from
    // bottom so we can surface the jump button when the user scrolls up.
    // Toggling via state is fine — the button only re-renders, not the
    // bubble list.
    //
    // Also doubles as a release signal for scroll-only gestures (scrollbar
    // drag/gutter, momentum). Our own pin writes emit `scroll` too, but they
    // set programmaticScrollRef just before the write — so we consume that
    // flag and ignore them. A scroll that wasn't ours AND lands the user more
    // than AT_BOTTOM_PX from the bottom is genuine intent → release the pin.
    const onScroll = () => {
      const distance = c.scrollHeight - c.scrollTop - c.clientHeight;
      setShowJumpLatest(distance > AT_BOTTOM_PX * 4);
      if (programmaticScrollRef.current) {
        programmaticScrollRef.current = false;
        return;
      }
      if (firstLoadPendingRef.current && distance > AT_BOTTOM_PX) {
        firstLoadPendingRef.current = false;
        setAutoPinActive(false);
      }
    };
    c.addEventListener("scroll", onScroll, { passive: true });
    return () => {
      window.clearInterval(pollId);
      if (rafId != null) cancelAnimationFrame(rafId);
      c.removeEventListener("load", onMediaLoad, true);
      c.removeEventListener("wheel", release);
      c.removeEventListener("touchmove", release);
      c.removeEventListener("keydown", release);
      c.removeEventListener("scroll", onScroll);
    };
  }, [hasMessages]);

  // Auto-load older when the top sentinel scrolls into view. Same pattern
  // as ChatList's infinite scroll, just inverted (top instead of bottom).
  //
  // Crucially we ignore intersections while `firstLoadPendingRef` is true
  // — on initial render the container is empty so the sentinel is
  // trivially visible, and without this gate the very first paint would
  // immediately fire loadOlder and yank the user away from the newest
  // messages before they could see them.
  useEffect(() => {
    const el = topSentinelRef.current;
    if (!el || !hasOlder) return;
    const io = new IntersectionObserver(
      (entries) => {
        if (firstLoadPendingRef.current) return;
        if (entries.some((e) => e.isIntersecting) && hasOlder && !loadingOlder) {
          onLoadOlder();
        }
      },
      { root: containerRef.current, rootMargin: "200px 0px 0px 0px" },
    );
    io.observe(el);
    return () => io.disconnect();
  }, [hasOlder, loadingOlder, onLoadOlder]);

  // Eager-set: the two media tiles (most recent inbound + most recent
  // outbound) that fetch immediately on chat open. Everything else
  // lazy-loads via the per-tile IntersectionObserver. Recomputes only
  // when the messages array changes — the set is tiny so the downstream
  // .has() lookups in MediaStrip are O(1).
  const eagerMediaIds = useMemo(
    () => pickEagerMediaIds(messages, ownerUserId),
    [messages, ownerUserId],
  );

  // id → message lookup for resolving `replyToMessageId` in O(1). Built once
  // per messages change instead of an O(n) `all.find` inside the render
  // flatMap (which was O(n²) across the whole list).
  const byId = useMemo(() => {
    const m = new Map<number, OFMessage>();
    for (const msg of messages) {
      const id = Number(msg.id);
      if (Number.isFinite(id)) m.set(id, msg);
    }
    return m;
  }, [messages]);

  // Empty / loading / error placeholders share the SAME flex sizing as
  // the populated branch below — without `flex-1 min-h-0` the column
  // collapses to the placeholder's natural height and the Composer
  // floats up into the middle of the pane until messages arrive.
  if (isLoading && messages.length === 0) {
    return (
      <div className="flex-1 min-h-0 grid place-items-center p-8 text-center text-sm text-fg-dim">
        Loading messages…
      </div>
    );
  }
  // A refresh that fails is only fatal when there is NOTHING to show. If we
  // already hold cached history, the thread stays readable and the failure
  // becomes a strip at the top of it (below the AI status strip in the header)
  // — see `staleNotice` in the populated branch. A fan who deletes his OF
  // account 404s every poll forever; blanking the whole conversation on that
  // threw away the only remaining copy of it.
  if (isError && messages.length === 0) {
    return (
      <div className="flex-1 min-h-0 grid place-items-center p-8 text-center text-sm text-err">
        {describeLoadError(error, "This fan's OnlyFans account no longer exists.")}
      </div>
    );
  }
  if (messages.length === 0) {
    return (
      <div className="flex-1 min-h-0 grid place-items-center p-8 text-center text-sm text-fg-dim">
        No messages yet.
      </div>
    );
  }

  return (
    <div className="flex-1 min-h-0 min-w-0 relative">
      {/* Auto-pin indicator (briefly during the initial 5s window) +
       *  "↓ Latest" floating button (whenever the user is scrolled up).
       *  Both sit in an absolute layer over the scroll container so they
       *  don't shift the message column. Fix #11 — gives the user a
       *  visible cue that the view is heading to the latest message, and
       *  a manual escape if they get stranded mid-history. */}
      {autoPinActive && (
        <div className="pointer-events-none absolute top-2 left-1/2 -translate-x-1/2 z-10 text-[10px] px-2 py-0.5 rounded-full bg-bg-elev-1/90 border border-border text-fg-dim opacity-80">
          ↓ Jumping to latest…
        </div>
      )}
      {showJumpLatest && (
        <button
          type="button"
          onClick={() => {
            const c = containerRef.current;
            if (!c) return;
            c.scrollTo({ top: c.scrollHeight, behavior: "smooth" });
          }}
          className="absolute bottom-3 right-4 z-10 text-[11px] px-2.5 py-1 rounded-full bg-accent text-bg shadow-md hover:opacity-90"
          title="Scroll to the latest message"
        >
          ↓ Latest
        </button>
      )}
    <div
      ref={containerRef}
      // overflow-anchor: none — Chrome's scroll-anchoring will fight our
      // manual pin when media loads above the fold ("bounce to middle"
      // bug, fix #11). We own scroll position via the effects above.
      style={{ overflowAnchor: "none" }}
      className="h-full overflow-y-auto overflow-x-hidden px-2 md:px-4 py-3 space-y-2"
    >
      {/* The refresh failed but we still have history — say so WITHOUT taking
       *  the thread away. `sticky` (not `absolute`) so it never covers the
       *  first message and never shifts the column: it rides the top of the
       *  scroll viewport and scrolls with the content underneath it. */}
      {isError && (
        <div
          role="status"
          className="sticky top-0 z-20 -mx-2 md:-mx-4 -mt-3 mb-1 px-3 py-1.5 text-[11px] leading-snug
                     bg-err/10 text-err border-b border-err/30 backdrop-blur-sm"
        >
          {describeLoadError(error, "This fan's OnlyFans account no longer exists.")}{" "}
          <span className="text-fg-dim">Showing the last messages we saved.</span>
        </div>
      )}
      {/* Sentinel for IntersectionObserver auto-load + an explicit
       *  click-to-load button. Scroll-up alone isn't enough when the
       *  visible page is short (a chat with only a few recent messages
       *  doesn't produce a scrollbar, so the IO never fires); the button
       *  gives an unconditional way to pull older history. */}
      <div ref={topSentinelRef} className="h-1" />
      {hasOlder ? (
        <div className="flex justify-center py-2">
          <button
            type="button"
            onClick={() => { if (!loadingOlder) onLoadOlder(); }}
            disabled={loadingOlder}
            className={cn(
              "text-[11px] px-3 py-1 rounded-full border border-border bg-bg-elev-1/40",
              "text-fg-dim hover:text-fg hover:bg-bg-elev-1 transition-colors",
              loadingOlder && "opacity-60 cursor-wait",
            )}
            title="Load older messages"
          >
            {loadingOlder ? "Loading older…" : "↑ Load older messages"}
          </button>
        </div>
      ) : messages.length > 0 ? (
        <div className="text-center py-2 text-[11px] text-fg-dim italic">
          Start of conversation.
        </div>
      ) : null}
      {messages.flatMap((m, i, all) => {
        // Resolve the bubble side WITHOUT waiting on the async owner-id
        // fetch (the old `fromUser.id === ownerUserId` was null on first
        // paint → every bubble started left then jumped right). See
        // resolveSide: explicit `_side` (DB-seed rows from `direction`)
        // wins; live rows fall back to ownerUserId, which the parent now
        // hydrates synchronously from a per-account localStorage cache.
        const isOutgoing = resolveSide(m, ownerUserId) === "right";
        const isOptimisticOutgoing = (m._tempId ?? 0) < 0;
        // Day-boundary separator: show "today" / "yesterday" / "23rd May 2026"
        // above the first message of each new local day, walking top-down.
        const prevCreatedAt = i > 0 ? all[i - 1].createdAt : null;
        const showDateSep =
          !!m.createdAt && (
            !prevCreatedAt ||
            localDateKey(prevCreatedAt) !== localDateKey(m.createdAt)
          );
        // Resolve the quoted message. OF sometimes only sends the id; fall
        // back to a cache walk in that case. Skipping the search when
        // `replyToMessage` is already populated keeps the common path cheap.
        let replyTo = m.replyToMessage ?? null;
        if (!replyTo && m.replyToMessageId) {
          const found = byId.get(m.replyToMessageId);
          if (found) {
            replyTo = {
              id: Number(found.id),
              text: found.text,
              fromUser: found.fromUser,
              createdAt: found.createdAt,
              mediaCount: found.mediaCount,
            };
          }
        }
        // ✓✓ seen logic: only outgoing real-server messages with an id ≤ the
        // peer's lastRead pointer count as read. Optimistic ids (≤0) and
        // future-scheduled bubbles don't get a tick — they're not on OF yet.
        const numericId = typeof m.id === "number" ? m.id : Number(m.id);
        const isRealOutgoing = isOutgoing && Number.isFinite(numericId) && numericId > 0;
        const seenByPeer =
          isRealOutgoing && lastReadByPeerId != null && numericId <= lastReadByPeerId;
        // Outbound "Sent by {name}" label: prefer the server-side attribution
        // map (covers all chatters across the team); fall back to the picker's
        // current employee for optimistic bubbles whose real id isn't in the
        // map yet. Truthy display_name only — pre-DB-branch and deleted-
        // employee rows render nothing rather than "Sent by null".
        let employeeLabel: string | null = null;
        if (isOutgoing || isOptimisticOutgoing) {
          const entry = attribution?.[String(m.id)];
          // An automation send resolves display_name to the flat "Automation"
          // sentinel — prefer the specific automation name ("AI Chat",
          // "Auto-reply", …) so the bubble says which one actually sent it.
          const autoName = automationLabel(entry?.automation_kind);
          if (entry?.sender_name) {
            // Human mass broadcast: the backend denormalized "mass-kingsley1"
            // into sender_name (empty for 1:1 sends and automation-fired mass,
            // so this branch only fires for a human blast). Prefer it verbatim.
            employeeLabel = entry.sender_name;
          } else if (autoName) {
            employeeLabel = autoName;
          } else if (entry?.display_name) {
            employeeLabel = entry.display_name;
          } else if (isOptimisticOutgoing && currentEmployeeName) {
            employeeLabel = currentEmployeeName;
          }
        }
        const bubble = (
          <Bubble
            key={String(m.id)}
            msg={m}
            replyTo={replyTo}
            isOutgoing={isOutgoing}
            isOptimisticOutgoing={isOptimisticOutgoing}
            accountId={accountId}
            fanId={fanId ?? null}
            ownerUserId={ownerUserId}
            onRetry={onRetry}
            onCancelScheduled={onCancelScheduled}
            onToggleLike={onToggleLike}
            onQuoteReply={onQuoteReply}
            onTogglePin={onTogglePin}
            onUnsend={onUnsend}
            onSendReward={onSendReward}
            onJumpTo={(id) => {
              // Tapping the quote card scrolls to the original. Same logic
              // as search-result click: set highlightId via the existing
              // jump path — but since Bubble lives inside MessageList, we
              // simulate it by dispatching a click via DOM scrollIntoView.
              const el = containerRef.current?.querySelector(
                `[data-msg-id="${CSS.escape(String(id))}"]`,
              ) as HTMLElement | null;
              el?.scrollIntoView({ behavior: "smooth", block: "center" });
            }}
            highlighted={highlightId != null && String(highlightId) === String(m.id)}
            isRealOutgoing={isRealOutgoing}
            seenByPeer={seenByPeer}
            eagerMediaIds={eagerMediaIds}
            employeeLabel={employeeLabel}
            translation={translateOn && m.text ? translations.get(stripHtml(m.text)) ?? null : null}
            imageDesc={imageDesc?.[String(m.id)] ?? null}
          />
        );
        if (!showDateSep) return [bubble];
        return [
          <DateSeparator
            key={`sep-${m.id}`}
            label={fmtDateSeparator(m.createdAt!)}
          />,
          bubble,
        ];
      })}
    </div>
    </div>
  );
}

function Bubble({
  msg, replyTo, isOutgoing, isOptimisticOutgoing, accountId, fanId, ownerUserId,
  onRetry, onCancelScheduled,
  onToggleLike, onQuoteReply, onTogglePin, onUnsend, onSendReward, onJumpTo,
  highlighted, isRealOutgoing, seenByPeer,
  eagerMediaIds, employeeLabel, translation, imageDesc,
}: {
  msg: OFMessage;
  replyTo: OFMessage["replyToMessage"] | null;
  isOutgoing: boolean;
  isOptimisticOutgoing: boolean;
  accountId: string | null;
  fanId: number | null;
  ownerUserId: number | null;
  onRetry: (tempId: number) => void;
  onCancelScheduled?: (tempId: number) => void;
  onToggleLike?: (msg: OFMessage) => void;
  onQuoteReply?: (msg: OFMessage) => void;
  onTogglePin?: (msg: OFMessage) => void;
  onUnsend?: (msg: OFMessage) => void;
  onSendReward?: (msg: OFMessage) => void;
  onJumpTo?: (messageId: number) => void;
  highlighted?: boolean;
  isRealOutgoing?: boolean;
  seenByPeer?: boolean;
  eagerMediaIds: Set<number>;
  employeeLabel?: string | null;
  /** English translation of this bubble's text (null = none/failed/off).
   *  Only rendered when the detected language isn't already English. */
  translation?: BubbleTranslation | null;
  /** What the AI saw in this (inbound) photo/gif; null = not described. */
  imageDesc?: string | null;
}) {
  const bubbleRef = useRef<HTMLDivElement | null>(null);
  // Touch reveal for the per-message toolbar. On phones there is no hover, so
  // the toolbar is hidden AND inert (pointer-events-none) until the bubble is
  // tapped. At md this state is ignored entirely — the md: classes restore the
  // original hover-only behaviour.
  const [touchOpen, setTouchOpen] = useState(false);
  // Scroll into view when search picks this bubble. Defer to next tick so
  // the container's own scroll-restore effect doesn't fight us for the
  // scrollTop slot.
  useEffect(() => {
    if (!highlighted) return;
    const id = setTimeout(() => {
      bubbleRef.current?.scrollIntoView({ behavior: "smooth", block: "center" });
    }, 50);
    return () => clearTimeout(id);
  }, [highlighted]);

  const [compactMedia] = useCompactMedia();
  // Optimistic messages can't yet have a real fromUser.id matching
  // ownerUserId — treat tempId<0 as "I sent this".
  const side = isOutgoing || isOptimisticOutgoing ? "right" : "left";
  const failed = !!msg._failed;
  const pending = !!msg._pending && !failed;
  const scheduled = !!msg._isFutureScheduled;
  // OF only lets you like the OTHER party's messages. Hide the affordance
  // on outgoing bubbles even though `isLiked` may still appear there (if
  // the fan reacted to our message).
  const canLike = !!onToggleLike && !isOutgoing && !isOptimisticOutgoing && !scheduled;
  const canReply = !!onQuoteReply && !scheduled && !failed && !pending;
  // Pin works on both directions (unlike `like`, which is incoming-only).
  // Optimistic / failed / scheduled bubbles can't be pinned — they have
  // no real OF message id yet.
  const numericIdForPin = typeof msg.id === "number" ? msg.id : Number(msg.id);
  const canPin = !!onTogglePin && !scheduled && !failed && !pending
    && Number.isFinite(numericIdForPin) && numericIdForPin > 0;
  // Unsend: only on outbound bubbles with a real OF id. OF enforces the
  // ~24h edit window upstream — we surface a friendly error if the
  // request 4xxs rather than gating client-side, since the precise
  // window isn't echoed on the message payload.
  const canUnsend = !!onUnsend && (isOutgoing || isOptimisticOutgoing)
    && !scheduled && !failed && !pending
    && Number.isFinite(numericIdForPin) && numericIdForPin > 0;
  // Manual tip-reward override: only on a real INCOMING tip bubble (a fan
  // tips us). Fires the tip_reward automation on demand — see onSendReward.
  const canSendReward = !!onSendReward && !!msg.isTip
    && !isOutgoing && !isOptimisticOutgoing && !scheduled && !failed && !pending
    && Number.isFinite(numericIdForPin) && numericIdForPin > 0;

  function onBubbleDoubleClick() {
    if (!canLike) return;
    onToggleLike!(msg);
  }

  // PPV: any positive price + isFree=false means this bubble was sold.
  // OF reports `isOpened=true` once the fan has paid. We render the
  // price tag + lock-state for both sides — outbound so the chatter
  // can see what they sent, inbound (rare; only on tip-back) for parity.
  const isPPV = !!msg.price && msg.price > 0;
  // Unlocked when EITHER OF reports it opened OR our ledger confirmed the
  // purchase (is_paid). The ledger stamp lands minutes before OF's isOpened
  // echoes back on a fresh fetch, so this makes a paid PPV unlock on the spot
  // instead of lingering "locked" on a cached bubble.
  const unlocked = isPPV && (!!msg.isOpened || !!msg.isPaid);
  // Outgoing PPV: we (the sender) own the content, so show thumbnails
  // even when the fan hasn't paid. Only incoming locked PPV shows the 🔒 tile.
  const locked = isPPV && !unlocked && !(isOutgoing || isOptimisticOutgoing);

  return (
    <div
      ref={bubbleRef}
      className={cn(
        "flex group",
        side === "right" ? "justify-end" : "justify-start",
        highlighted && "rounded-md ring-2 ring-info/60 transition-shadow",
      )}
    >
      <div className="max-w-[88%] md:max-w-[75%] flex flex-col gap-1 relative">
        {/* Hover toolbar — sits above the bubble. Only shows when the
         *  message is in a state where actions make sense. */}
        {(canReply || canLike || canPin || canUnsend || canSendReward) && (
          <div
            className={cn(
              "absolute -top-7 transition-opacity",
              "flex items-center gap-1 bg-panel border border-border rounded-md shadow-sm px-1 py-0.5 z-10",
              touchOpen ? "opacity-100" : "opacity-0 pointer-events-none",
              "md:opacity-0 md:group-hover:opacity-100 md:pointer-events-auto",
              side === "right" ? "right-1" : "left-1",
            )}
          >
            {canReply && (
              <button
                type="button"
                onClick={() => onQuoteReply!(msg)}
                className="text-[11px] px-2 py-2 md:text-[10px] md:px-1.5 md:py-0.5 rounded hover:bg-bg-elev-1 text-fg-dim hover:text-fg"
                title="Quote-reply"
              >
                ↩ reply
              </button>
            )}
            {canLike && (
              <button
                type="button"
                onClick={() => onToggleLike!(msg)}
                className={cn(
                  "text-[11px] px-2 py-2 md:text-[10px] md:px-1.5 md:py-0.5 rounded hover:bg-bg-elev-1",
                  msg.isLiked ? "text-err" : "text-fg-dim hover:text-fg",
                )}
                title={msg.isLiked ? "Unlike" : "Like (or double-click bubble)"}
              >
                {msg.isLiked ? "♥" : "♡"} like
              </button>
            )}
            {canPin && (
              <button
                type="button"
                onClick={() => onTogglePin!(msg)}
                className={cn(
                  "text-[11px] px-2 py-2 md:text-[10px] md:px-1.5 md:py-0.5 rounded hover:bg-bg-elev-1",
                  msg.isPinned ? "text-accent" : "text-fg-dim hover:text-fg",
                )}
                title={msg.isPinned ? "Unpin from chat" : "Pin to chat"}
              >
                {msg.isPinned ? "📌 pinned" : "📌 pin"}
              </button>
            )}
            {canUnsend && (
              <button
                type="button"
                onClick={() => onUnsend!(msg)}
                className="text-[11px] px-2 py-2 md:text-[10px] md:px-1.5 md:py-0.5 rounded hover:bg-err/10 text-fg-dim hover:text-err"
                title="Unsend (within OF's 24h window)"
              >
                🗑 unsend
              </button>
            )}
            {canSendReward && (
              <button
                type="button"
                onClick={() => onSendReward!(msg)}
                className="text-[11px] px-2 py-2 md:text-[10px] md:px-1.5 md:py-0.5 rounded hover:bg-info/10 text-fg-dim hover:text-info"
                title="Send a free vault reward for this tip (fires the tip_reward automation)"
              >
                🎁 reward
              </button>
            )}
          </div>
        )}
        {isPPV && (
          <div
            className={cn(
              "text-[10px] font-medium px-1.5 py-0.5 rounded-md inline-flex items-center gap-1 self-start",
              side === "right" ? "self-end" : "self-start",
              unlocked
                ? "bg-ok/15 text-ok border border-ok/30"
                : "bg-warn/15 text-warn border border-warn/30",
            )}
          >
            <span aria-hidden>{unlocked ? "✓" : "🔒"}</span>
            <span>${msg.price!.toFixed(2)}</span>
            <span className="opacity-70">{unlocked ? "unlocked" : "locked"}</span>
          </div>
        )}
        <div
          onDoubleClick={onBubbleDoubleClick}
          onClick={() => setTouchOpen((v) => !v)}
          data-msg-id={String(msg.id)}
          className={cn(
            "relative rounded-2xl px-3 py-2 text-sm whitespace-pre-wrap break-words",
            side === "right"
              ? "bg-accent text-white rounded-br-md"
              : "bg-bg-elev-1 text-fg rounded-bl-md",
            // Fan tip ("I sent you a $X.XX tip") — paint the bubble blue so
            // incoming money stands out at a glance, on either side.
            msg.isTip && "!bg-info/15 !text-info !border !border-info/40",
            failed && "ring-1 ring-err/60",
            pending && !scheduled && "opacity-70",
            // Local-wait scheduled bubble: dashed outline + faded fill so
            // it's visually obvious this hasn't actually been sent yet.
            scheduled && side === "right" &&
              "!bg-accent/10 !text-accent border-2 border-dashed border-accent/50",
            canLike && "cursor-default select-text",
          )}
        >
          {replyTo && (
            <QuoteCard
              replyTo={replyTo}
              side={side}
              ownerUserId={ownerUserId}
              onClick={() => onJumpTo?.(replyTo.id)}
            />
          )}
          {/* A pending AI reply carries no body — rhythm defers before the reply
           *  LLM runs, so there is nothing drafted yet to preview. The bubble
           *  would otherwise render empty and read as a bug, so say what the gap
           *  actually is; the footer below carries the wake time. */}
          {msg._aiPending && !msg.text && (
            <span className="inline-flex items-center gap-1.5 italic text-fg-dim">
              <span className="animate-pulse">•••</span>
              pausing before replying
            </span>
          )}
          {msg.text && (
            <span className="inline-flex flex-wrap items-baseline gap-1.5 align-baseline">
              {/* Per-text FREE / PAID badge — only on outbound PPV bubbles.
               *  Free messages don't need a "FREE" tag (the bubble already
               *  has no PPV pill above it, so it'd be pure noise). */}
              {(isOutgoing || isOptimisticOutgoing) && isPPV && (
                <span
                  className={cn(
                    "px-1 rounded text-[9px] font-bold leading-tight text-white shadow self-center",
                    msg.lockedText ? "bg-err" : "bg-fg-dim",
                  )}
                  title={msg.lockedText ? "Text locked — fan must pay to read" : "Text was sent unlocked"}
                >
                  {msg.lockedText ? "PAID" : "FREE"}
                </span>
              )}
              <TranslatableText raw={msg.text} translation={translation} />
            </span>
          )}
          {msg.media?.length ? <MediaStrip msg={msg} locked={locked} accountId={accountId} fanId={fanId} isOutgoing={isOutgoing || isOptimisticOutgoing} eagerMediaIds={eagerMediaIds} /> : null}
          {/* Who OF has on record as tagged in this send. Rendered from
           *  `releaseForms` — what OF STORED — and never from what we asked
           *  for, because those two disagreed silently for as long as the chat
           *  body shipped its ids in `userTags`. An operator who tags someone
           *  needs to see the tag land, not see their own click echoed back. */}
          {msg.releaseForms?.length ? (
            <div className="mt-1 flex flex-wrap gap-1">
              {msg.releaseForms.map((rf) => (
                <span
                  key={rf.id}
                  className="inline-flex items-center gap-0.5 text-[10px] leading-tight px-1.5 py-0.5 rounded-full bg-black/15"
                  title={`Tagged on OnlyFans — ${rf.name || rf.id} (release form)`}
                >
                  <span aria-hidden>🏷</span>
                  {rf.name || `#${rf.id}`}
                </span>
              ))}
            </div>
          ) : null}
          {/* What the AI sees in what HE sent. Incoming only — an outbound bubble
           *  is our own send, and a locked PPV has nothing to read. Giphy dms carry
           *  NO media (just a giphyId), and they're exactly what the free title
           *  lane describes, so they must be covered here too. */}
          {!isOutgoing && !isOptimisticOutgoing && !locked
            && (msg.media?.length || msg.giphyId) ? (
            <ImageDescCaption
              accountId={accountId}
              fanId={fanId}
              messageId={numericIdForPin}
              desc={imageDesc ?? null}
              isGif={!msg.media?.length && !!msg.giphyId}
              media={msg.media}
            />
          ) : null}
          {msg.giphyId ? (
            <a
              href={`https://media.giphy.com/media/${msg.giphyId}/giphy.gif`}
              target="_blank"
              rel="noreferrer"
              className={cn(
                "block mt-1.5 rounded-md overflow-hidden",
                compactMedia ? "max-w-[100px]" : "max-w-[155px]",
              )}
            >
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img
                src={`https://media.giphy.com/media/${msg.giphyId}/giphy.gif`}
                alt="GIF"
                loading="lazy"
                className="w-full h-auto object-cover"
              />
            </a>
          ) : null}
          {/* Heart sticker — anchored to the bubble's bottom-corner so the
           *  read state stays attached to the message it belongs to. */}
          {msg.isLiked && (
            <span
              className={cn(
                "absolute -bottom-2 text-[13px] leading-none",
                side === "right" ? "-left-1" : "-right-1",
              )}
              title="Liked"
              aria-label="Liked"
            >
              <span className="inline-grid place-items-center w-4 h-4 rounded-full bg-panel border border-border text-err">
                ♥
              </span>
            </span>
          )}
          {/* Pin sticker — top corner mirror of the heart. Tells the
           *  chatter at a glance which messages are pinned without
           *  having to open the pinned popover. */}
          {msg.isPinned && (
            <span
              className={cn(
                "absolute -top-2 text-[11px] leading-none",
                side === "right" ? "-left-1" : "-right-1",
              )}
              title="Pinned to chat"
              aria-label="Pinned"
            >
              <span className="inline-grid place-items-center w-4 h-4 rounded-full bg-panel border border-border text-accent">
                📌
              </span>
            </span>
          )}
        </div>
        <div
          className={cn(
            "text-[10px] text-fg-dim px-1 flex flex-col gap-0.5",
            side === "right" ? "items-end" : "items-start",
          )}
        >
          <div className="flex items-center gap-1.5 flex-wrap">
          {scheduled
            ? <ScheduledStatus fireAt={msg._fireAt ?? msg.createdAt} mediaCount={msg.mediaCount ?? 0}
                               ai={msg._aiPending} />
            : <span>{fmtTime(msg.createdAt)}</span>}
          {/* Delivered / seen receipts on outgoing real-server messages.
           *  We can only assert "delivered" once OF has assigned a real
           *  id (so pending/failed bubbles get nothing). Seen comes from
           *  the chat-list's lastReadMessageId, surfaced by the parent. */}
          {isRealOutgoing && !pending && !failed && (
            <span
              className={cn(seenByPeer ? "text-info" : "text-fg-dim")}
              title={seenByPeer ? "Seen by recipient" : "Delivered"}
            >
              {seenByPeer ? "✓✓ seen" : "✓ delivered"}
            </span>
          )}
          {pending && !scheduled && <span className="text-warn">sending…</span>}
          {scheduled && onCancelScheduled && msg._scheduleJobId != null && (
            <button
              type="button"
              onClick={() => msg._scheduleJobId != null && onCancelScheduled(msg._scheduleJobId)}
              className="text-err hover:underline"
              title="Cancel this scheduled send (for everyone on the account)."
            >
              cancel
            </button>
          )}
          {failed && (
            <>
              <button
                type="button"
                onClick={() => msg._tempId != null && onRetry(msg._tempId)}
                className="text-err hover:underline"
              >
                failed — retry
              </button>
              {msg._failedReason && (
                <span className="text-err/80" title={msg._failedReason}>
                  · {truncate(msg._failedReason, 80)}
                </span>
              )}
              {/* OF's sentence never says WHICH attachment, so translate the
               *  ids it did give us into a count pointing at the marked tiles.
               *  Retrying the same bundle can only fail the same way. Gated on
               *  the media too: ids can only refer to tiles this bubble
               *  rendered, so with none there is nothing to count against. */}
              {msg._failedMediaIds?.length && msg.media?.length ? (
                <span
                  className="text-err font-medium"
                  title="OF refused these exact attachments — drop them and re-send; a plain retry sends the same bundle."
                >
                  · remove {msg._failedMediaIds.length} of {msg.media.length}
                </span>
              ) : null}
            </>
          )}
          </div>
          {/* Reserve the "Sent by …" line on every outbound bubble so it
           *  doesn't bounce/reflow when the async attribution map lands and
           *  the label pops in. Empty placeholder keeps the 10px line height;
           *  aria-hidden + a NBSP so it occupies space without being read. */}
          {/* Which automation/teammate sent it now shows on phone too — on a
           *  phone the human chatter can't tell an AI-Chatter bubble from a
           *  mass blast without it. Same "Sent by …" text as desktop; the
           *  anti-reflow NBSP placeholder rides along so labelless bubbles
           *  reserve the line identically across breakpoints. */}
          {(isOutgoing || isOptimisticOutgoing) && (
            <span className="inline">
              {employeeLabel ? (
                <span className="text-fg-dim/70" title={`Sent by ${employeeLabel}`}>
                  Sent by {employeeLabel}
                </span>
              ) : (
                <span className="text-fg-dim/70" aria-hidden>
                  &nbsp;
                </span>
              )}
            </span>
          )}
        </div>
      </div>
    </div>
  );
}

function MediaStrip({ msg, locked, accountId, fanId, isOutgoing, eagerMediaIds }: {
  msg: OFMessage;
  locked: boolean;
  accountId: string | null;
  fanId: number | null;
  isOutgoing: boolean;
  eagerMediaIds: Set<number>;
}) {
  const items = msg.media ?? [];
  const [blurMode] = useBlurMode();
  const [compactMedia] = useCompactMedia();
  const blurCls = blurImageClass(blurMode);
  // Per-fan vault history → drives the green BOUGHT badge on outbound
  // PPV tiles. Only the outbound side queries — inbound media is the
  // fan's tip-back and our purchase signal doesn't apply.
  const fanVaultQ = useFanVaultHistory(accountId, fanId, isOutgoing && fanId != null);
  const isPPV = !!msg.price && msg.price > 0;
  const previewIds = msg.previews ?? [];
  // Attachments OF itself refused on a failed send; empty on every other
  // message. Same shape and same membership test as `previewIds` above — both
  // are "which of these tiles is special", and a bundle is small enough that a
  // Set would be ceremony.
  const refusedIds = msg._failedMediaIds ?? [];
  // Price label fits in the same 5-char corner pill as "BOUGHT" / "FREE".
  const priceLabel = msg.price != null
    ? `$${msg.price % 1 === 0 ? msg.price : msg.price.toFixed(2)}`
    : "";
  // PPV gifts can attach 40+ items; rendering them all blows up the bubble
  // and floods the relay /img proxy. Cap at:
  //   • 2 tiles when the message is still locked (the lock icon repeats —
  //     beyond 2 it's pure noise)
  //   • 3 tiles for visible thumbnails (a clean 3-up row in the bubble)
  // The user expands to see the rest via the "+N more" pill.
  const LOCKED_CAP = 2;
  const IMAGE_CAP = 3;
  const cap = locked ? LOCKED_CAP : IMAGE_CAP;
  const [expanded, setExpanded] = useState(false);
  const visible = expanded ? items : items.slice(0, cap);
  const hiddenCount = items.length - visible.length;
  // Click-to-zoom lightbox. Holds the index into `items` of the tile being
  // viewed full-res (null = closed). visible[i] === items[i] (slice keeps
  // order from 0), so a visible tile's index maps straight through.
  const [lightboxIdx, setLightboxIdx] = useState<number | null>(null);
  // Tile geometry (T-GRID): media renders as small, fixed-footprint
  // thumbnails so a bundle never towers over the bubble. Each tile's box
  // is computed up-front by mediaTileBox() from the payload's width/height
  // — NOT from <img> onload — so the layout is definite from first paint
  // and never reflows when the async bytes land. The image and EVERY
  // placeholder (lock / processing / skeleton) read the SAME box, so the
  // swap-on-load causes zero movement. flex-wrap lets 1–3 little tiles sit
  // in a row and wrap; a single image is just one small box (no half-bubble
  // crop). The fallback box (used when OF didn't size the media) is also
  // what the "+N more" pill and any unknown-dim tile claim.
  const fallbackBox = mediaTileBox(compactMedia, null, null);
  return (
    <div className="mt-2">
      <div className="flex flex-wrap gap-1.5">
        {visible.map((m, i) => {
          const rawThumb = m.files?.thumb?.url || m.files?.source?.url || m.url || null;
          const thumb = proxyImage(rawThumb, accountId);
          const fullRaw = m.files?.source?.url || rawThumb;
          const full = proxyImage(fullRaw, accountId);
          // One source of truth for the tile footprint. Aspect-fit inside the
          // max box when OF gave us dims (box matches the true ratio, so
          // object-contain shows the whole image with no bars); a neutral
          // fallback box (letterboxed) when dims are null.
          const box = mediaTileBox(compactMedia, m.width, m.height);
          // Three-state badge mirrors the composer: green BOUGHT > gray FREE
          // (preview teaser) > red $X (PPV locked). Free-message outbound
          // (price=0) gets a badge only if the fan has bought it from us
          // at some earlier point.
          const fanEntry = m.id != null ? fanVaultQ.data?.by_media[String(m.id)] : undefined;
          // Only assert BOUGHT once the per-fan vault query has actually
          // resolved — otherwise a free (price=0) tile paints with no badge
          // and then flashes one in when the async lookup lands. `isSuccess`
          // distinguishes "not loaded" from "confirmed not bought".
          const wasBought = fanVaultQ.isSuccess && fanEntry?.was_purchased === true;
          const inPreviews = m.id != null && previewIds.includes(m.id);
          const showBadge = isOutgoing && (wasBought || isPPV);
          const badge = (
            showBadge ? (
              <div
                className={cn(
                  "absolute top-1 left-1 px-1 rounded text-[9px] font-bold leading-tight text-white shadow z-10 pointer-events-none",
                  wasBought
                    ? "bg-ok"
                    : inPreviews ? "bg-fg-dim" : "bg-err",
                )}
                title={
                  wasBought
                    ? "BOUGHT — this fan has purchased this item"
                    : inPreviews
                      ? "Free preview — sent unlocked"
                      : "PPV-locked — fan hasn't unlocked this tile"
                }
              >
                {wasBought ? "BOUGHT" : inPreviews ? "FREE" : priceLabel || "PPV"}
              </div>
            ) : null
          );
          // OF named this exact id in `payload.removeFromInputMediaIds`, so say
          // it ON the tile: the failure line below can only repeat OF's
          // sentence, which talks about "media" without saying which — and a
          // bundle of near-identical thumbs is exactly where that fails the
          // operator (lib/sendFailure records what it cost on 2026-08-16).
          const wasRefused = m.id != null && refusedIds.includes(m.id);
          // Everything painted ON the tile, as one node. The three branches
          // below differ in what they wrap, not in the chrome they carry, so
          // the chrome is composed once here — including the outline, which is
          // an inset element rather than a class so no branch has to grow a
          // conditional className for it.
          const overlays = (
            <>
              {badge}
              {wasRefused && (
                <>
                  <div className="absolute inset-0 rounded-md border-2 border-err pointer-events-none z-10" />
                  <div
                    className="absolute inset-x-0 bottom-0 bg-err/85 text-white text-[9px] font-bold leading-tight text-center py-0.5 z-10 pointer-events-none"
                    title="OF refused this attachment — remove it and the rest of the bundle sends"
                  >
                    REMOVE
                  </div>
                </>
              )}
            </>
          );
          if (locked) {
            // Generic lock placeholder (incoming, unpaid). Same box as a
            // loaded tile would claim — its real dims don't matter until the
            // fan unlocks it, so the fallback/aspect box is fine.
            return (
              <div
                key={m.id ?? i}
                style={{ width: box.w, height: box.h }}
                className="relative flex-none bg-black/30 rounded-md grid place-items-center text-[11px] leading-tight text-warn border border-warn/30 overflow-hidden"
              >
                {overlays}
                🔒
              </div>
            );
          }
          if (!thumb) {
            // Pre-thumb placeholder. Reserve the SAME box the loaded image
            // will take so the swap doesn't reflow.
            return (
              <div
                key={m.id ?? i}
                style={{ width: box.w, height: box.h }}
                className="relative flex-none bg-bg/30 rounded-md grid place-items-center text-center text-[9px] leading-tight px-1 opacity-70 overflow-hidden"
              >
                {overlays}
                …
              </div>
            );
          }
          const isEager = m.id != null && eagerMediaIds.has(m.id);
          return (
            <a
              key={m.id ?? i}
              href={full || thumb}
              target="_blank"
              rel="noreferrer"
              // Left-click opens the in-app full-res lightbox; ctrl/cmd/middle
              // click falls through to the href so power users can still pop a
              // raw tab. The href also keeps this keyboard-focusable.
              onClick={(e) => {
                if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
                e.preventDefault();
                setLightboxIdx(i);
              }}
              style={{ width: box.w, height: box.h }}
              // Fixed box (mediaTileBox) gives a definite footprint that the
              // skeleton and loaded image both fill via h-full, so the swap on
              // load causes zero reflow. object-contain shows the whole image:
              // for known dims the box matches the ratio (no bars); for unknown
              // dims it letterboxes inside the neutral fallback box.
              className="relative block flex-none overflow-hidden rounded-md bg-black/20 cursor-zoom-in"
            >
              {overlays}
              <MediaTile
                mediaId={m.id ?? null}
                proxiedUrl={thumb}
                fallbackUrl={rawThumb ?? ""}
                eager={isEager}
                alt=""
                className={cn("rounded-md border border-black/10 object-contain", blurCls)}
              />
            </a>
          );
        })}
        {hiddenCount > 0 && (
          <button
            type="button"
            onClick={() => setExpanded(true)}
            style={{ width: fallbackBox.w, height: fallbackBox.h }}
            className="flex-none rounded-md border border-border bg-bg-elev-1 hover:bg-bg-elev-2 text-xs text-fg-dim flex items-center justify-center gap-1"
          >
            +{hiddenCount}
          </button>
        )}
      </div>
      {/* "show less" sits OUTSIDE the tile row so it keeps its natural button
       *  height instead of inheriting a tile box. */}
      {expanded && items.length > cap && (
        <button
          type="button"
          onClick={() => setExpanded(false)}
          className="mt-1.5 w-full rounded-md border border-border bg-bg-elev-1 hover:bg-bg-elev-2 text-[11px] text-fg-dim py-1"
        >
          show less
        </button>
      )}
      {lightboxIdx != null && items[lightboxIdx] && (
        <MediaLightbox
          items={items}
          accountId={accountId}
          index={lightboxIdx}
          onClose={() => setLightboxIdx(null)}
          onIndex={setLightboxIdx}
        />
      )}
    </div>
  );
}

/** Full-resolution click-to-zoom overlay. Portaled to <body> so no ancestor
 *  transform/overflow can clip it. Click the dimmed backdrop (or the ×, or
 *  Esc) to close; clicking the image itself does NOT close. ←/→ (and the
 *  on-screen arrows) page through every item in the bundle, wrapping at the
 *  ends. Loads the source/full URL — a genuinely higher-res asset than the
 *  in-bubble thumbnail — via a plain <img> (the relay /img endpoint serves
 *  bytes straight to src; no need for MediaTile's hash/abort plumbing here). */
function MediaLightbox({
  items,
  accountId,
  index,
  onClose,
  onIndex,
}: {
  items: OFMedia[];
  accountId: string | null;
  index: number;
  onClose: () => void;
  onIndex: (next: number) => void;
}) {
  const [loaded, setLoaded] = useState(false);
  const count = items.length;
  const m = items[index];
  const fullRaw = m?.files?.source?.url || m?.files?.thumb?.url || m?.url || null;
  const full = proxyImage(fullRaw, accountId);

  // Reset the spinner whenever we page to a different image.
  useEffect(() => {
    setLoaded(false);
  }, [index]);

  // Esc closes; ←/→ page (wrap). Bound at the document so it works no matter
  // where focus landed after the tile click.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onClose();
      } else if (count > 1 && e.key === "ArrowLeft") {
        e.preventDefault();
        onIndex((index - 1 + count) % count);
      } else if (count > 1 && e.key === "ArrowRight") {
        e.preventDefault();
        onIndex((index + 1) % count);
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [index, count, onClose, onIndex]);

  if (!full) return null;

  return createPortal(
    <div
      className="fixed inset-0 z-[80] bg-black/85 backdrop-blur-sm flex items-center justify-center"
      onClick={onClose}
      role="dialog"
      aria-modal="true"
    >
      <button
        type="button"
        onClick={(e) => {
          e.stopPropagation();
          onClose();
        }}
        aria-label="Close image"
        className="absolute top-4 right-4 z-10 w-10 h-10 grid place-items-center rounded-full bg-white/10 hover:bg-white/20 text-white text-2xl leading-none"
      >
        ×
      </button>

      {count > 1 && (
        <>
          <button
            type="button"
            aria-label="Previous image"
            onClick={(e) => {
              e.stopPropagation();
              onIndex((index - 1 + count) % count);
            }}
            className="absolute left-3 top-1/2 -translate-y-1/2 z-10 w-11 h-11 grid place-items-center rounded-full bg-white/10 hover:bg-white/20 text-white text-3xl leading-none"
          >
            ‹
          </button>
          <button
            type="button"
            aria-label="Next image"
            onClick={(e) => {
              e.stopPropagation();
              onIndex((index + 1) % count);
            }}
            className="absolute right-3 top-1/2 -translate-y-1/2 z-10 w-11 h-11 grid place-items-center rounded-full bg-white/10 hover:bg-white/20 text-white text-3xl leading-none"
          >
            ›
          </button>
          <div className="absolute bottom-4 left-1/2 -translate-x-1/2 z-10 text-white/80 text-xs bg-black/40 rounded-full px-3 py-1">
            {index + 1} / {count}
          </div>
        </>
      )}

      {/* Clicking the image must NOT close — only the backdrop does. */}
      <div className="relative max-w-[92vw] max-h-[92vh]" onClick={(e) => e.stopPropagation()}>
        {!loaded && (
          <div className="absolute inset-0 grid place-items-center text-white/70 text-sm">
            loading…
          </div>
        )}
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img
          src={full}
          alt=""
          onLoad={() => setLoaded(true)}
          className={cn(
            "max-w-[92vw] max-h-[92vh] object-contain rounded-md transition-opacity duration-150",
            loaded ? "opacity-100" : "opacity-0",
          )}
        />
      </div>
    </div>,
    document.body,
  );
}

/** Aspect threshold above which media counts as "portrait" (tall). In compact
 *  mode tall media gets the narrow 40px-wide tile (see mediaTileBox) instead
 *  of being squished into the 50px landscape strip. A value >1 keeps near-
 *  square media in the landscape lane so only clearly-tall media is narrowed. */
const PORTRAIT_MIN_RATIO = 1.2;

/** Whether a media item counts as tall (portrait), from its natural pixel
 *  dimensions. width/height are NULLABLE by design — OF doesn't always size
 *  media and the client-measured dims PATCH is best-effort — so a null / zero
 *  / landscape / square item returns false. Exported as a testable seam and
 *  consumed by mediaTileBox to pick the narrow-portrait compact tile. */
export function isPortraitMedia(
  width?: number | null,
  height?: number | null,
): boolean {
  if (width == null || height == null) return false;
  if (width <= 0 || height <= 0) return false;
  return height >= width * PORTRAIT_MIN_RATIO;
}

/** Side length (px) of an in-bubble media tile. compact → small, normal →
 *  larger. The two tile sizes are the only knobs. */
const COMPACT_TILE_PX = 64;
const NORMAL_TILE_PX = 155;

/** Displayed pixel footprint of an in-bubble media tile. Pure + exported as
 *  the testable seam for the T-GRID layout: the image AND every placeholder
 *  (lock / processing / skeleton) read the SAME box, so the async byte swap
 *  never reflows.
 *
 *  EVERY tile is a FIXED SQUARE — the size depends ONLY on the compact toggle,
 *  NOT on the media's width/height. This is deliberate: OF dimensions arrive
 *  late and best-effort (the client-measured PATCH lands after first paint),
 *  so any box derived from them would RESIZE when the dims show up — that's
 *  the lazy-load bounce. A constant square is reserved identically before and
 *  after the bytes/dims arrive, so messages never shift. The image fills the
 *  square via object-contain (whole image, letterboxed) so nothing is cropped.
 *  width/height are accepted (and ignored) so the signature stays stable. */
export function mediaTileBox(
  compact: boolean,
  _width?: number | null,
  _height?: number | null,
): { w: number; h: number } {
  const side = compact ? COMPACT_TILE_PX : NORMAL_TILE_PX;
  return { w: side, h: side };
}

function QuoteCard({
  replyTo, side, ownerUserId, onClick,
}: {
  replyTo: NonNullable<OFMessage["replyToMessage"]>;
  side: "left" | "right";
  ownerUserId: number | null;
  onClick: () => void;
}) {
  // Decide who originated the quoted message — same direction test the
  // host bubble uses. Label "You" vs the author name so the chatter can
  // tell at a glance whether they're replying to their own send or the fan.
  const fromOwner =
    ownerUserId != null && replyTo.fromUser?.id === ownerUserId;
  const author =
    fromOwner
      ? "You"
      : replyTo.fromUser?.name || replyTo.fromUser?.username || "fan";
  const preview = replyTo.text
    ? stripHtml(replyTo.text)
    : replyTo.mediaCount
    ? `(${replyTo.mediaCount} media)`
    : "(message)";
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "block w-full text-left mb-1.5 rounded-md border-l-2 pl-2 py-1 pr-2 text-[11px] leading-tight",
        side === "right"
          ? "bg-white/10 border-white/60 hover:bg-white/15"
          : "bg-black/10 border-fg/40 hover:bg-black/15",
      )}
      title="Jump to quoted message"
    >
      <div className={side === "right" ? "font-semibold text-white" : "font-semibold text-fg"}>
        {author}
      </div>
      <div className={cn("truncate", side === "right" ? "text-white/85" : "text-fg-dim")}>
        {preview || "(empty)"}
      </div>
    </button>
  );
}

/** Which side a bubble sits on, resolved WITHOUT comparing ids against an
 *  async fetch. Priority:
 *   1. An explicit `_side` carried on the row — DB-seed rows derive it from
 *      the authoritative `direction` column (mapped in useChatMessagesLocal),
 *      so seeded text never has to wait on the owner id.
 *   2. Optimistic outbound (tempId < 0) — always ours, even before OF echoes
 *      a real fromUser.id.
 *   3. Live OF rows (no `direction`): compare fromUser.id to ownerUserId.
 *      ownerUserId is now hydrated synchronously from a per-account
 *      localStorage cache in ChatSurface, so the FIRST paint already has it
 *      and there is no left-then-right jump.
 *  relay.ts owns OFMessage; `_side` rides along as an optional intersection
 *  field rather than widening that shared type. */
function resolveSide(
  m: OFMessage,
  ownerUserId: number | null,
): "left" | "right" {
  const explicit = (m as { _side?: "left" | "right" })._side;
  if (explicit === "left" || explicit === "right") return explicit;
  if ((m._tempId ?? 0) < 0) return "right";
  return ownerUserId != null && m.fromUser?.id === ownerUserId ? "right" : "left";
}

function truncate(s: string, max: number): string {
  return s.length > max ? s.slice(0, max - 1) + "…" : s;
}

/** OF returns message bodies as HTML — strip tags AND decode the most
 *  common entities so bubbles render as plain text without leaking
 *  `<br>` / `&amp;` artifacts. We avoid `dangerouslySetInnerHTML` so
 *  malformed input from OF can never inject markup into our chat. */
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
    .replace(/&#39;/g, "'");
}

// Per-language color for the "(es)" tag — same code, same color everywhere so
// a chatter learns "amber = Spanish" at a glance. Unknown codes fall back to sky.
const LANG_TAG_COLOR: Record<string, string> = {
  es: "text-amber-400",
  sl: "text-violet-400",
  pt: "text-emerald-400",
  fr: "text-sky-400",
  de: "text-orange-400",
  it: "text-teal-400",
};

/** Bubble text with the 🌐 translate layer. When a translation exists and the
 *  detected language isn't English, render "(es)" (colored by language) + the
 *  English text; the original stays one hover away in the title tooltip.
 *  English / untranslated / toggle-off all render the plain original. */
function TranslatableText({ raw, translation }: {
  raw: string;
  translation?: BubbleTranslation | null;
}) {
  const original = stripHtml(raw);
  const t = translation;
  const show = !!t?.text && t.lang !== "en"
    && t.text.trim().toLowerCase() !== original.trim().toLowerCase();
  if (!show) return <span>{original}</span>;
  return (
    <>
      <span
        className={cn(
          "text-[9px] font-bold self-center tracking-wide",
          LANG_TAG_COLOR[t.lang] ?? "text-sky-400",
        )}
      >
        ({t.lang})
      </span>
      <span title={original}>{t.text}</span>
    </>
  );
}

function fmtTime(iso?: string): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function localDateKey(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`;
}

function ordinalSuffix(n: number): string {
  const v = n % 100;
  if (v >= 11 && v <= 13) return "th";
  switch (n % 10) {
    case 1: return "st";
    case 2: return "nd";
    case 3: return "rd";
    default: return "th";
  }
}

function fmtDateSeparator(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const now = new Date();
  if (localDateKey(iso) === `${now.getFullYear()}-${now.getMonth()}-${now.getDate()}`) {
    return "today";
  }
  const y = new Date();
  y.setDate(y.getDate() - 1);
  if (localDateKey(iso) === `${y.getFullYear()}-${y.getMonth()}-${y.getDate()}`) {
    return "yesterday";
  }
  const day = d.getDate();
  const month = d.toLocaleString(undefined, { month: "long" });
  return `${day}${ordinalSuffix(day)} ${month} ${d.getFullYear()}`;
}

function DateSeparator({ label }: { label: string }) {
  return (
    <div
      className="flex items-center gap-2 my-2 select-none"
      role="separator"
      aria-label={label}
    >
      <div className="flex-1 h-px bg-border" />
      <span className="text-[10px] uppercase tracking-wider text-fg-dim px-1">
        {label}
      </span>
      <div className="flex-1 h-px bg-border" />
    </div>
  );
}

const AI_PAUSE_TITLE =
  "His message has been seen; the reply is pausing before it goes (Human Rhythm). Not cancellable.";

/** Live countdown that re-renders every 20s. Short enough to feel
 *  responsive ("Sending in 1 min" → fires shortly after); long enough
 *  to not be wasteful across many pending bubbles in one chat. */
function ScheduledStatus({ fireAt, mediaCount = 0, ai = false }:
                         { fireAt: string; mediaCount?: number; ai?: boolean }) {
  const [, tick] = useState(0);
  useEffect(() => {
    const id = setInterval(() => tick((n) => n + 1), 20_000);
    return () => clearInterval(id);
  }, []);
  const clip = mediaCount > 0 ? ` · 📎 ${mediaCount}` : "";
  const ms = new Date(fireAt).getTime() - Date.now();
  const mins = Math.max(1, Math.round(ms / 60_000));
  // An elapsed AI countdown isn't stuck — the executor ticks every ~30s and takes
  // one job per (account, kind) — so "writing" is the honest word for it.
  const label = ms <= 0
    ? (ai ? "🤖 writing…" : `⏱ sending now…${clip}`)
    : (ai ? `🤖 AI reply · ${fmtTime(fireAt)} (in ${mins} min)`
          : `⏱ scheduled · ${fmtTime(fireAt)} (in ${mins} min)${clip}`);
  return <span className="text-accent" title={ai ? AI_PAUSE_TITLE : undefined}>{label}</span>;
}
