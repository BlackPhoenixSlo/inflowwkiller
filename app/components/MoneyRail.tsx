"use client";

/**
 * MoneyRail — sticky bottom-right dock for money notifications (purchases
 * + tips) so chatters can jump straight to the fans who just paid.
 *
 * TWO components, on purpose. `MoneyRail` is a gate: it owns the surface and
 * the settings and nothing else, and mounts `MoneyRailPanel` only where the
 * rail is meant to appear. Everything below — the fetches, the dock, the
 * drag — lives in the panel and may assume it is on a surface the user asked
 * for. See the gate's own note for why that boundary has to be a mount and
 * can't be a flag.
 *
 * It fetches OF's /notifications?type=purchases|tip on mount — collapsed or
 * not — so the rail is fresh on first page load rather than only after the
 * user expands it. Those rows are merged with the localStorage notif-history
 * mirror (which carries SSE arrivals that OF hasn't listed yet). Uses the SAME
 * ["notif-list", aid, type] query keys as the bell, so the cache is shared and
 * the toaster's invalidation on SSE arrival refreshes this list too.
 *
 * COLD START: a page refresh wipes the react-query cache, and each model's
 * OF fan-out lands at its own pace — so every successful fetch is mirrored
 * into a localStorage snapshot, and the next mount shows those rows
 * immediately (dimmed) while each model revalidates independently. The rail
 * never blanks back to "Loading…" just because the tab reloaded.
 *
 * WHICH MODELS: every active one by default, deliberately ignoring the
 * ScopeContext the rest of the UI follows — money landing on the model you're
 * NOT looking at is exactly what you don't want to miss. Narrow it in ⚙.
 *
 * Two sizes, and the marker (">" / "⌄") points where a click takes you.
 * Collapsing SHRINKS THE WINDOW, it doesn't truncate the feed — every row
 * stays mounted and scrolls, so "small" shows 1 row (configurable 0-4, where
 * 0 collapses to just the header bar) with the rest a flick away, and "big"
 * shows 6 (configurable 5-8). Which SURFACES show the rail at all (inbox /
 * solo chat pop-out / group tab / everything else) is also a ⚙ choice — the
 * house default keeps it on the chat surfaces and off the admin pages. A
 * rail hidden on the current surface is restorable from the 🔔 dropdown,
 * which is mounted everywhere. The header always carries the count of sales
 * that arrived since the rows were last on screen.
 *
 * Draggable by its header. Position + the collapsed/expanded bit are persisted
 * PER SURFACE (inbox / chat pop-out / group tab): the rail is mounted once in
 * the root layout, but a corner that's out of the way in the inbox can cover a
 * pane in the group tab, so each surface remembers its own. Row counts and the
 * model pick are global — they're about the work, not this window's layout.
 * Dragging over the header's buttons doesn't fire them.
 *
 * Sticky by design: it never closes on outside click or Escape — only
 * tapping the header toggles it. Rows deep-link to the fan's chat with
 * the one-shot ?refresh=media flag (same as money toasts) since the fan
 * just moved money and the popout's persisted caches are stale by seconds.
 * Each fan gets ONE named popout tab (lib/chatPopout) — re-clicking a row
 * re-navigates + focuses that tab with a fresh load instead of stacking
 * another _blank tab that paints the stale localStorage snapshot.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { usePathname } from "next/navigation";
import { useQueries, useQueryClient } from "@tanstack/react-query";

import { chatTabName, openChatTab } from "@/lib/chatPopout";
import { useActiveAccounts } from "@/hooks/useAccounts";
import { useRailSettings } from "@/hooks/useRailSettings";
import { relay, type OFChatItem } from "@/lib/relay";
import { proxyImage } from "@/lib/mediaUrl";
import { compareFanIds, type FanId, sameUserId } from "@/lib/fanId";
import { stripHtmlPreview } from "@/lib/htmlPreview";
import { cn } from "@/lib/utils";
import type { AttributionEntry, AttributionResponse } from "@/hooks/useChatAttribution";
import { GROUP_SLOT_CAP, openGroupTabWithSlots, type GroupSlot } from "@/lib/groupChannel";
import {
  mapOfTypeToKey,
  readNotifHistory,
  type CachedNotification,
  type NotifTypeKey,
} from "@/lib/notifSettings";
import {
  BIG_ROW_CHOICES,
  DEFAULT_DOCK,
  SEEN_KEY,
  SMALL_ROW_CHOICES,
  SNAPSHOT_MAX_ITEMS,
  parseSurfaces,
  railVisibleOn,
  readDock,
  readMoneySnapshot,
  readSeenTs,
  resolveRailAccounts,
  slimNotif,
  snapshotKeyOf,
  surfaceOf,
  unseenCountOf,
  SURFACE_VALUES,
  writeDock,
  writeMoneySnapshot,
  writeSeenTs,
  type DockState,
  type NotificationItem,
  type RailSettings,
  type SnapshotMap,
  type Surface,
} from "@/lib/moneyRailStorage";

const MONEY_TYPES = ["purchases", "tip"] as const;

const MAX_ROWS = 50;
const PANEL_W = 290;
/** One row's height. The body's max-height is rows × this, so "small = 1 row"
 *  is a window onto the feed, not a truncation — the list still scrolls. */
const ROW_H = 68;
/** Non-scrolling chrome above the list, used to cap the body so an expand
 *  can't run off the screen edge. Estimates, not measurements — measuring
 *  would need a post-render pass feeding back into the height that caused it. */
const HEADER_H = 38;
const SETTINGS_H = 280;
/** Breathing room kept between the panel and the far viewport edge. */
const VIEWPORT_MARGIN = 12;
/** The default (never-dragged) corner sits `bottom-3 right-3` = 12px in. */
const DEFAULT_EDGE_GAP = 12;

/** How deep we fetch chat detail (last text + seen state): what's visible plus
 *  a little lookahead, since the window scrolls. One OF roundtrip per fan, so
 *  it's hard-capped — rows past it render without the status line. */
const DETAIL_ROWS_LOOKAHEAD = 3;
const DETAIL_ROWS_MAX = 12;
/** Floor between two forced re-fetches of the same fan's chat detail. */
const DETAIL_REFRESH_MIN_GAP_MS = 10_000;
/** Pointer slop before a press on the header counts as a drag and not a click. */
const DRAG_THRESHOLD_PX = 4;

/** Which edge a panel occupying [top, top+height] should pin to: whichever the
 *  panel's centre is nearer. */
export function anchorFor(top: number, height: number, viewportH: number): "top" | "bottom" {
  return top + height / 2 > viewportH / 2 ? "bottom" : "top";
}

/** Keep the panel on screen — after a drag, a resize, or a restore into a
 *  window smaller than the one the position was saved from. Always leaves the
 *  header grabbable. */
export function clampToViewport(x: number, y: number, panelH: number): { x: number; y: number } {
  const maxX = Math.max(0, window.innerWidth - PANEL_W);
  const maxY = Math.max(0, window.innerHeight - Math.min(panelH, 80));
  return {
    x: Math.min(Math.max(0, x), maxX),
    y: Math.min(Math.max(0, y), maxY),
  };
}

interface MergedItem {
  n: NotificationItem;
  accountId: string;
  typeKey: NotifTypeKey;
  ts: number;
}

/** Where the conversation with this payer stands right now.
 *  `outbound` = WE spoke last, so nobody owes the fan anything. */
export interface ReplyState {
  text: string;
  outbound: boolean;
  /** Outbound only: the fan's lastReadMessageId has reached our last msg. */
  seen: boolean;
  /** Inbound only: their message is still unread by us. */
  unread: boolean;
  /** Outbound only: an automation sent it, not a person. The thread still
   *  wants a human, so it keeps the orange dot. */
  byBot: boolean;
  /** The chat-list cache knows a message the detail fetch hasn't seen yet
   *  (we just sent, or one just arrived over SSE) — so the detail fetch,
   *  and with it the read receipt, is behind. The component refetches. */
  stale: boolean;
}

/** The newest outbound row from the attribution endpoint. `automationKind`
 *  is null when a human chatter sent it. */
export function lastOutboundOf(
  resp: AttributionResponse | undefined,
): { messageId: FanId; automationKind: string | null } | null {
  const entries = Object.entries(resp?.by_msg_id ?? {});
  if (entries.length === 0) return null;
  // limit=1, but don't depend on that — take the highest id.
  //
  // The id stays the exact key text. `Number(id)` rounded every Fansly
  // snowflake (951605852249276416 -> ...400), and the rounded value was then
  // `===`-compared against the wire's string id below — a comparison that is
  // false for BOTH reasons at once, so `byBot` never fired on Fansly and the
  // rail could not tell a bot's reply from a chatter's.
  let best: { messageId: FanId; automationKind: string | null } | null = null;
  for (const [id, entry] of entries) {
    if (!/^\d+$/.test(id)) continue;
    if (!best || compareFanIds(id, best.messageId) > 0) {
      best = { messageId: id, automationKind: (entry as AttributionEntry).automation_kind ?? null };
    }
  }
  return best;
}

function msgTs(m?: { createdAt?: string } | null): number {
  return m?.createdAt ? Date.parse(m.createdAt) || 0 : 0;
}

/** Derive the seen / owes-a-reply state for one fan. Mirrors the inbox's
 *  rule: a `lastMessage.fromUser.id` that isn't ours means the FAN spoke last
 *  and we owe them a reply (the orange dot), and `lastReadMessageId >=
 *  lastMessage.id` means they read what we sent (the blue ✓✓).
 *
 *  Two sources, neither one sufficient:
 *    • `detail`  — the per-fan /chats/{id} fetch. Authoritative ids and the
 *      read receipt, but up to 60s stale.
 *    • `cached`  — the inbox chat-list row. Patched OPTIMISTICALLY on send
 *      and on SSE arrival, so it's the only source that reflects a message
 *      sent seconds ago — but that patch reuses the previous `lastMessage.id`
 *      (useSendMessage), so its id cannot be trusted for the receipt compare.
 *
 *  So: newest `createdAt` wins for the text, and a message newer than the
 *  detail fetch is by definition not yet seen (the receipt can't have caught
 *  up), which also avoids reading the stale optimistic id. Returns null for
 *  an empty thread. */
export function replyStateOf(
  detail: OFChatItem | null,
  cached: OFChatItem | null,
  accountId: string,
  lastOutbound: { messageId: FanId; automationKind: string | null } | null = null,
): ReplyState | null {
  const dLm = detail?.lastMessage ?? null;
  const cLm = cached?.lastMessage ?? null;
  // Strictly newer, so a tie keeps the trustworthy detail row.
  const cacheIsNewer = !!cLm && msgTs(cLm) > msgTs(dLm);
  const lm = cacheIsNewer ? cLm : (dLm ?? cLm);
  if (!lm) return null;

  // Receipts only move forward, so the higher of the two is the truth.
  //
  // Ordered with compareFanIds, NOT Math.max: a receipt id is a snowflake on
  // Fansly, and Math.max forces it through float64 (…416 -> …400) before it is
  // compared against the message id. Fansly sends no receipts yet, which is the
  // only reason this never showed as a wrong ✓✓ — it would have the day they do.
  const lastReadCandidates = [detail?.lastReadMessageId, cached?.lastReadMessageId]
    .filter((v): v is NonNullable<typeof v> => v != null && String(v) !== "0");
  const lastRead: FanId | null = lastReadCandidates.length
    ? lastReadCandidates.reduce((a, b) => (compareFanIds(a, b) >= 0 ? a : b))
    : null;
  // String-normalized compare: a Fansly `fromUser.id` is a string snowflake,
  // so `Number(accountId)` never matched it and every answered thread still
  // reported "they spoke last" (orange dot).
  const outbound = sameUserId(lm.fromUser?.id, accountId);
  const text = lm.text?.trim()
    ? stripHtmlPreview(lm.text, 60)
    : lm.mediaCount
      ? `(${lm.mediaCount} media)`
      : "";
  // `stale` only when the detail fetch actually exists and is behind — a fan
  // the detail fetch hasn't loaded at all isn't stale, just uncovered.
  const stale = cacheIsNewer && !!dLm;

  // An automation answering doesn't discharge the thread — only a person
  // does. We can only claim "a bot sent it" when the attribution row we have
  // is FOR this exact message; if the ids don't line up (the send hasn't been
  // written to our DB yet, i.e. `stale`), stay silent rather than flash a
  // false orange dot at a chatter who just replied.
  const byBot =
    outbound &&
    !stale &&
    lm.id != null &&
    lastOutbound != null &&
    sameUserId(lastOutbound.messageId, lm.id) &&
    !!lastOutbound.automationKind;

  return {
    text,
    outbound,
    seen: outbound && !stale && lastRead != null && lm.id != null && compareFanIds(lastRead, lm.id) >= 0,
    unread: !outbound && ((cached?.unreadMessagesCount ?? detail?.unreadMessagesCount ?? 0) > 0
      || !!(cached?.hasUnread ?? detail?.hasUnread)),
    byBot,
    stale,
  };
}

function notifPath(type: string): string {
  const q = new URLSearchParams({ limit: "20", offset: "0", type });
  return `/api/of/v2/users/notifications?${q.toString()}`;
}

function parseList(raw: unknown): NotificationItem[] {
  if (Array.isArray(raw)) return raw as NotificationItem[];
  if (raw && typeof raw === "object" && Array.isArray((raw as { list?: unknown }).list)) {
    return (raw as { list: NotificationItem[] }).list;
  }
  return [];
}

/**
 * Money-rail rescue hatch: hiding the rail on a surface takes its ⚙ (the
 * only un-hide control) down with it. The 🔔 bell is mounted everywhere, so
 * it renders this inside its dropdown to offer the way back when the rail is
 * hidden on the CURRENT surface. Self-contained — it owns the surface read,
 * the hidden-here check and the restore write, and returns null when the rail
 * isn't hidden here, so hosts can mount it unconditionally.
 */
export function MoneyRailRestoreButton() {
  const surface = surfaceOf(usePathname());
  const { settings, commit } = useRailSettings();
  const showHere = () => {
    if (!settings) return;
    // Adding the last missing surface normalises to null (= everywhere).
    commit({
      ...settings,
      surfaces: parseSurfaces([...(settings.surfaces ?? []), surface]),
    });
  };
  if (!settings || railVisibleOn(settings, surface)) return null;
  return (
    <button
      type="button"
      onClick={showHere}
      className="w-full text-left px-3 py-1.5 text-[11px] border-b border-border text-fg-dim hover:text-fg hover:bg-bg-elev-1 flex items-center gap-1.5"
      title="The Buys & tips rail is hidden on this page — bring it back"
    >
      <span aria-hidden>💰</span>
      <span className="truncate">
        Buys &amp; tips rail is hidden here —{" "}
        <span className="text-info">show it on this page</span>
      </span>
    </button>
  );
}

/**
 * The rail is mounted once in the ROOT layout, so it is on every route, but
 * `settings.surfaces` decides where it actually appears. That decision lives
 * HERE, in a parent that owns nothing else, rather than inside the panel:
 * hooks can't be conditional, so a panel that judged its own visibility would
 * judge it only AFTER every fetch, effect, listener and cache subscription it
 * declares had already fired — which is exactly what it used to do, paying
 * ~14 OF round-trips on each of the 8 routes where it then painted nothing.
 * A gate makes that structurally impossible instead of individually gated:
 * on a surface the user didn't tick, MoneyRailPanel never mounts, so there is
 * nothing to remember to switch off.
 */
export function MoneyRail() {
  const surface = surfaceOf(usePathname());
  const { settings, commit } = useRailSettings();
  if (!settings || !railVisibleOn(settings, surface)) return null;
  return <MoneyRailPanel surface={surface} settings={settings} onSettingsChange={commit} />;
}

function MoneyRailPanel({
  surface,
  settings,
  onSettingsChange,
}: {
  surface: Surface;
  settings: RailSettings;
  onSettingsChange: (next: RailSettings) => void;
}) {
  const activeAccounts = useActiveAccounts();
  const [settingsOpen, setSettingsOpen] = useState(false);

  // Dock state is per-surface, so it can't be read during the first render
  // (the server doesn't know the surface OR localStorage). Hydration-gate the
  // whole panel and load in an effect keyed by surface — a client-side nav
  // between /inbox and /group re-reads the other surface's saved spot.
  const [dock, setDock] = useState<DockState>(DEFAULT_DOCK);
  const [mounted, setMounted] = useState(false);
  // Needed to cap the body height against the viewport. Kept in state (not read
  // during render) so SSR and the first client render agree.
  const [viewportH, setViewportH] = useState(0);
  // Last session's fetched rows, served as query placeholders below. Loaded
  // once per mount — after the first real fetches land, it's inert.
  const [snapshot, setSnapshot] = useState<SnapshotMap>({});
  // Timestamp of the newest money row the user has actually had on screen.
  const [seenTs, setSeenTs] = useState(0);
  useEffect(() => {
    setDock(readDock(surface));
    setSnapshot(readMoneySnapshot());
    // First-ever use: nothing that predates the rail counts as "new" — a
    // fresh install greeting the user with "50 new" is noise, not signal.
    let seen = readSeenTs();
    if (!seen) {
      seen = Date.now();
      writeSeenTs(seen);
    }
    setSeenTs(seen);
    setViewportH(window.innerHeight);
    setMounted(true);
  }, [surface]);

  const open = dock.open;
  const panelRef = useRef<HTMLDivElement | null>(null);

  const commitDock = useCallback((next: DockState) => {
    setDock(next);
    writeDock(surface, next);
  }, [surface]);

  // A press on the header is either a drag or a click; we can't know until the
  // pointer moves. Track it, and swallow the click that a drag inevitably
  // fires so releasing the pointer over the toggle doesn't also collapse it.
  const dragRef = useRef<
    { startX: number; startY: number; origX: number; origY: number; moved: boolean } | null
  >(null);
  const draggedRef = useRef(false);

  // Drag is tracked on WINDOW listeners, never with setPointerCapture on the
  // header. Capturing the pointer also retargets the compatibility mouse
  // events — the subsequent `click` would be delivered to the capturing header
  // instead of the button under the cursor, which silently kills every button
  // in the header bar.
  const onHeaderPointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    if (e.button !== 0) return;
    const rect = panelRef.current?.getBoundingClientRect();
    if (!rect) return;
    dragRef.current = {
      startX: e.clientX, startY: e.clientY,
      origX: rect.left, origY: rect.top,
      moved: false,
    };

    // While dragging we position from the TOP — it follows the pointer 1:1 and
    // needs no knowledge of the panel's height. The anchor is decided once, on
    // drop, and the top offset is converted to a bottom offset if needed.
    const onMove = (ev: PointerEvent) => {
      const d = dragRef.current;
      if (!d) return;
      const dx = ev.clientX - d.startX;
      const dy = ev.clientY - d.startY;
      if (!d.moved && Math.hypot(dx, dy) < DRAG_THRESHOLD_PX) return;
      d.moved = true;
      draggedRef.current = true;
      const h = panelRef.current?.offsetHeight ?? ROW_H;
      const { x, y } = clampToViewport(d.origX + dx, d.origY + dy, h);
      setDock((cur) => ({ ...cur, x, y, anchor: "top" }));
    };

    const onUp = () => {
      const d = dragRef.current;
      dragRef.current = null;
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("pointercancel", onUp);
      if (!d?.moved) return;
      // The click that follows this pointerup (if we released over a button)
      // must be swallowed. But a drag released over dead space fires NO click,
      // and a `draggedRef` left armed would eat the user's next real click. A
      // 0ms timer runs after click dispatch but before any later interaction.
      window.setTimeout(() => { draggedRef.current = false; }, 0);

      const rect = panelRef.current?.getBoundingClientRect();
      setDock((cur) => {
        let next = cur;
        if (rect) {
          const anchor = anchorFor(rect.top, rect.height, window.innerHeight);
          next = {
            ...cur,
            anchor,
            // Re-express the offset against the edge we just pinned to.
            y: anchor === "bottom"
              ? Math.max(0, window.innerHeight - rect.bottom)
              : Math.max(0, rect.top),
          };
        }
        writeDock(surface, next);
        return next;
      });
    };

    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointercancel", onUp);
  };

  const toggle = () => {
    // The pointerup that ended a drag also lands here as a click. Eat it once.
    if (draggedRef.current) {
      draggedRef.current = false;
      return;
    }
    commitDock({ ...dock, open: !dock.open });
  };

  // Restoring a saved spot into a smaller window (or resizing after a drag)
  // could park the dock off-screen with no way to grab it back.
  useEffect(() => {
    if (!mounted) return;
    const onResize = () => {
      setViewportH(window.innerHeight);
      setDock((cur) => {
        if (cur.x == null || cur.y == null) return cur;
        const h = panelRef.current?.offsetHeight ?? ROW_H;
        // clampToViewport works in top-space, so a bottom-anchored offset has
        // to be converted in and back out — clamping it directly would treat a
        // distance-from-bottom as a distance-from-top.
        const top = cur.anchor === "bottom" ? window.innerHeight - cur.y - h : cur.y;
        const c = clampToViewport(cur.x, top, h);
        const y = cur.anchor === "bottom"
          ? Math.max(0, window.innerHeight - (c.y + h))
          : c.y;
        if (c.x === cur.x && y === cur.y) return cur;
        const next = { ...cur, x: c.x, y };
        writeDock(surface, next);
        return next;
      });
    };
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, [mounted, surface]);

  // NOTE: no useScope() here, by design. The rail watches every active model
  // by default even when the rest of the UI is scoped to one — money landing
  // on another model is exactly what you don't want to miss. Narrow it in the
  // rail's own settings if you really only care about a subset.
  const targetAccountIds = useMemo<string[]>(
    () => resolveRailAccounts(settings, activeAccounts),
    [settings, activeAccounts],
  );

  // Re-render whenever the SSE toaster mirrors a new arrival into the
  // localStorage history (same-tab custom event + cross-tab storage event).
  const [historyTick, bumpHistory] = useState(0);
  useEffect(() => {
    const bump = () => bumpHistory((n) => n + 1);
    const onStorage = (e: StorageEvent) => {
      if (e.key === "chatterly:notif-history:v1") bump();
      // Another tab saw the rows — this tab's "new" badge clears too.
      if (e.key === SEEN_KEY) setSeenTs(readSeenTs());
    };
    window.addEventListener("chatterly:notif-history", bump);
    window.addEventListener("storage", onStorage);
    return () => {
      window.removeEventListener("chatterly:notif-history", bump);
      window.removeEventListener("storage", onStorage);
    };
  }, []);

  // OF fan-out — one query per account × money type. Gated only on hydration
  // (the snapshot below is a localStorage read); the surface question was
  // already answered by MoneyRail, or this panel wouldn't exist. Deliberately
  // NOT gated on `open` either: a COLLAPSED rail still renders `smallRows`
  // live rows. Query keys intentionally match the bell's so both share one
  // cache entry per (account, type).
  const queryDefs = useMemo(
    () => targetAccountIds.flatMap((aid) => MONEY_TYPES.map((type) => ({ aid, type }))),
    [targetAccountIds],
  );
  const listQueries = useQueries({
    queries: queryDefs.map(({ aid, type }) => ({
      queryKey: ["notif-list", aid, type] as const,
      queryFn: async () => parseList(await relay.get(notifPath(type), { accountId: aid })),
      enabled: mounted,
      staleTime: 30_000,
      refetchOnWindowFocus: false,
      // Cold start: show this account's rows from the last session at once
      // (dimmed) while the real fetch runs, instead of blanking the rail.
      placeholderData: () => snapshot[snapshotKeyOf(aid, type)]?.items,
    })),
  });

  const dataSig = listQueries.map((q) => q.dataUpdatedAt).join(",");

  // Mirror every real fetch into the snapshot for the NEXT page load.
  // Read-merge-write so accounts outside the current pick keep their rows.
  useEffect(() => {
    if (!mounted) return;
    const map = readMoneySnapshot();
    let dirty = false;
    listQueries.forEach((q, i) => {
      const def = queryDefs[i];
      if (!def || !q.data || q.isPlaceholderData || !q.dataUpdatedAt) return;
      const key = snapshotKeyOf(def.aid, def.type);
      if (map[key]?.at === q.dataUpdatedAt) return;
      map[key] = { at: q.dataUpdatedAt, items: q.data.slice(0, SNAPSHOT_MAX_ITEMS).map(slimNotif) };
      dirty = true;
    });
    if (dirty) writeMoneySnapshot(map);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mounted, dataSig]);

  // Accounts still on placeholder rows — their fetch hasn't landed yet, so
  // their rows render dimmed until it does. Not memoised: trivially cheap,
  // and listQueries is a fresh array every render anyway.
  const placeholderAids = new Set<string>();
  listQueries.forEach((q, i) => {
    if (q.isPlaceholderData) {
      const def = queryDefs[i];
      if (def) placeholderAids.add(def.aid);
    }
  });
  const anyRefreshing = listQueries.some((q) => q.isFetching);

  const merged = useMemo<MergedItem[]>(() => {
    const acc: MergedItem[] = [];
    const seenIds = new Set<string>();
    listQueries.forEach((q, i) => {
      const def = queryDefs[i];
      if (!def) return;
      for (const n of q.data ?? []) {
        const ts = n.createdAt ? Date.parse(n.createdAt) || 0 : 0;
        if (n.id != null) seenIds.add(`${def.aid}:${n.id}`);
        acc.push({ n, accountId: def.aid, typeKey: def.type, ts });
      }
    });
    const history = readNotifHistory();
    for (const aid of targetAccountIds) {
      const items: CachedNotification[] = history[aid] ?? [];
      for (const c of items) {
        const key = c.typeKey ?? mapOfTypeToKey(c.type);
        if (key !== "tip" && key !== "purchases") continue;
        if (seenIds.has(`${aid}:${c.id}`)) continue;
        const ts = c.createdAt ? Date.parse(c.createdAt) || 0 : 0;
        acc.push({
          n: {
            id: c.id,
            type: c.type,
            text: c.text,
            createdAt: c.createdAt,
            user: c.user,
            replacePairs: c.replacePairs,
          },
          accountId: aid,
          typeKey: key,
          ts,
        });
      }
    }
    acc.sort((a, b) => b.ts - a.ts);
    return acc.slice(0, MAX_ROWS);
    // `snapshot` is a dep because placeholder rows surface through q.data
    // WITHOUT touching dataUpdatedAt — when the snapshot loads on mount,
    // nothing else here changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dataSig, queryDefs, targetAccountIds, historyTick, snapshot]);

  // Header count of not-yet-seen sales. The watermark advances only while
  // rows are actually rendered (≥1 visible row on a shown surface) — a
  // bar-only dock or a hidden surface lets it climb instead. Scrolling depth
  // is deliberately ignored: rows-on-screen counts as seen.
  const unseen = unseenCountOf(merged, seenTs);
  useEffect(() => {
    if (!mounted || merged.length === 0) return;
    if ((open ? settings.bigRows : settings.smallRows) === 0) return;
    const newest = merged[0]?.ts ?? 0; // merged is sorted newest-first
    if (newest > seenTs) {
      writeSeenTs(newest);
      setSeenTs(newest);
    }
  }, [mounted, merged, open, settings, seenTs]);

  // ── Per-fan chat detail: the last message in the thread + whether the
  // fan has read it. OF's /chats/{fanId} carries `lastMessage` and
  // `lastReadMessageId` (the highest message id the fan marked read) —
  // the same pair the inbox uses for its ✓✓ / awaiting-reply markers.
  // Only for the rows we actually render, deduped by fan.
  const detailTargets = useMemo(() => {
    // Fetch a little past what's visible (the window scrolls), but never for
    // all 50 — each row is an OF roundtrip. At 0 collapsed rows this still
    // pre-warms the lookahead, so expanding doesn't start from nothing.
    const want = (open ? settings.bigRows : settings.smallRows) + DETAIL_ROWS_LOOKAHEAD;
    const rows = merged.slice(0, Math.min(want, DETAIL_ROWS_MAX));
    const out: { aid: string; fanId: FanId }[] = [];
    const seen = new Set<string>();
    for (const m of rows) {
      const fanId = m.n.user?.id ?? m.n.fromUser?.id;
      if (!fanId) continue;
      const key = `${m.accountId}:${fanId}`;
      if (seen.has(key)) continue;
      seen.add(key);
      out.push({ aid: m.accountId, fanId });
    }
    return out;
  }, [merged, open, settings.bigRows, settings.smallRows]);

  const chatQueries = useQueries({
    queries: detailTargets.map(({ aid, fanId }) => ({
      queryKey: ["chat-detail", aid, fanId] as const,
      // 'background': up to DETAIL_ROWS_MAX (12) OF calls per account, re-fired
      // on every SSE money event. Nobody is waiting on the rail's read-receipt
      // ticks, and at 'user' priority this fan-out crowds out the actual click
      // in the relay's per-account lane.
      queryFn: () => relay.get<OFChatItem>(
        `/api/of/v2/chats/${fanId}`, { accountId: aid, priority: "background" },
      ),
      enabled: mounted,
      staleTime: 60_000,
      refetchOnWindowFocus: false,
    })),
  });

  // The inbox's chat-list rows are the source we KNOW carries
  // `lastReadMessageId` (ChatSurface drives its ✓✓ off exactly this). When
  // the inbox is loaded these rows are free and already fresh, so we prefer
  // them for the read receipt and fall back to the per-fan detail fetch.
  const qc = useQueryClient();
  const [chatsCacheTick, setChatsCacheTick] = useState(0);
  useEffect(() => qc.getQueryCache().subscribe((e) => {
    if (e.query.queryKey?.[0] === "chats" && e.type === "updated") {
      queueMicrotask(() => setChatsCacheTick((t) => t + 1));
    }
  }), [qc]);

  const cachedChats = useMemo(() => {
    type Page = { rows: OFChatItem[] };
    type Infinite = { pages?: Page[] };
    const map = new Map<string, OFChatItem>();
    for (const q of qc.getQueryCache().findAll({ queryKey: ["chats"] })) {
      const data = q.state.data as Infinite | undefined;
      for (const p of data?.pages ?? []) {
        for (const c of p.rows ?? []) {
          const aid = c.__accountId ?? "";
          if (aid && c.withUser?.id) map.set(`${aid}:${c.withUser.id}`, c);
        }
      }
    }
    return map;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [qc, chatsCacheTick]);

  // Who sent the last outbound message — a chatter or an automation. Served
  // from our own SQLite (no OF roundtrip): `automation_kind` is stamped on
  // every bot send and is null for a human. limit=1 gives just the newest
  // outbound row, which is all the rail needs.
  const attribQueries = useQueries({
    queries: detailTargets.map(({ aid, fanId }) => ({
      queryKey: ["money-rail-attrib", aid, fanId] as const,
      queryFn: () => relay.get<AttributionResponse>(
        `/admin/messages/${encodeURIComponent(aid)}/${fanId}/attribution?limit=1`,
      ),
      enabled: mounted,
      staleTime: 30_000,
      refetchOnWindowFocus: false,
    })),
  });

  const chatSig = chatQueries.map((q) => q.dataUpdatedAt).join(",");
  const attribSig = attribQueries.map((q) => q.dataUpdatedAt).join(",");
  const replyStates = useMemo(() => {
    const map = new Map<string, ReplyState>();
    detailTargets.forEach((t, i) => {
      const key = `${t.aid}:${t.fanId}`;
      const detail = chatQueries[i]?.data ?? null;
      const cached = cachedChats.get(key) ?? null;
      const st = replyStateOf(detail, cached, t.aid, lastOutboundOf(attribQueries[i]?.data));
      if (st) map.set(key, st);
    });
    return map;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [chatSig, attribSig, detailTargets, cachedChats]);

  // When the chat-list cache shows a message the detail fetch doesn't know
  // about (we just sent one, or SSE delivered one), the detail row — and so
  // the read receipt — is behind. Pull it again. Rate-limited per fan because
  // OF can keep echoing the old lastMessage for a beat after a send, and an
  // unguarded invalidate-on-stale would spin.
  const refreshedAtRef = useRef<Map<string, number>>(new Map());
  useEffect(() => {
    const now = Date.now();
    // Iterate the TARGETS, not the string keys. The id is never reconstructed
    // from text here: `detailTargets` already holds the exact `{aid, fanId}`
    // the queries above were keyed with, so invalidation matches by
    // construction on both platforms. Round-tripping through the `aid:fanId`
    // string cannot do that — react-query hashes 12345 and "12345" to
    // different keys, so parsing the half back with `Number()` missed every
    // Fansly snowflake, and returning it as text missed every numeric
    // OnlyFans id. Either way the stale-detail refresh silently never fired
    // and the read-receipt tick + attribution dot stayed stale.
    for (const { aid, fanId } of detailTargets) {
      const key = `${aid}:${fanId}`;
      const st = replyStates.get(key);
      if (!st?.stale) continue;
      const last = refreshedAtRef.current.get(key) ?? 0;
      if (now - last < DETAIL_REFRESH_MIN_GAP_MS) continue;
      refreshedAtRef.current.set(key, now);
      void qc.invalidateQueries({ queryKey: ["chat-detail", aid, fanId] });
      // The attribution row for a just-sent message is written when OF echoes
      // it, so it lands a beat after the optimistic patch. Refetch it too, or
      // a chatter's reply keeps the bot's `automation_kind` from the previous
      // message and the dot never clears.
      void qc.invalidateQueries({ queryKey: ["money-rail-attrib", aid, fanId] });
    }
  }, [replyStates, detailTargets, qc]);

  // The newest GROUP_SLOT_CAP distinct payers, in the rail's own order
  // (most recent money first) — what the 👥 button seeds the group tab with.
  const groupSlots = useMemo<GroupSlot[]>(() => {
    const out: GroupSlot[] = [];
    const seen = new Set<string>();
    for (const m of merged) {
      const fanId = m.n.user?.id ?? m.n.fromUser?.id;
      if (!fanId) continue;
      const key = `${m.accountId}:${fanId}`;
      if (seen.has(key)) continue;
      seen.add(key);
      out.push({ accountId: m.accountId, fanId });
      if (out.length >= GROUP_SLOT_CAP) break;
    }
    return out;
  }, [merged]);

  const anyLoading = listQueries.some((q) => q.isLoading);
  const showAccountTag = targetAccountIds.length > 1;
  const accountLabel = useMemo(() => {
    const m = new Map<string, string>();
    for (const a of activeAccounts) m.set(a.id, a.nickname || a.id);
    return m;
  }, [activeAccounts]);

  if (!mounted || targetAccountIds.length === 0) return null;

  // Collapsed no longer truncates the feed — it just shrinks the window onto
  // it. Every row stays in the DOM and scrolls, so one row is visible but the
  // rest are a flick away.
  const placed = dock.x != null && dock.y != null;

  // Expanding must not push the panel past the edge it's growing toward, so
  // the body is capped by the room actually available on the free side. `y` is
  // measured from the anchored edge, so the free side is the rest of the
  // viewport either way. Chrome (header + the open ⚙ panel) is estimated
  // rather than measured — measuring it would need a post-render pass that
  // feeds back into the height that caused it.
  const chromeH = HEADER_H + (settingsOpen ? SETTINGS_H : 0);
  const edgeGap = placed ? dock.y! : DEFAULT_EDGE_GAP;
  const room = viewportH - edgeGap - chromeH - VIEWPORT_MARGIN;
  // 0 visible rows (collapsed, smallRows=0) drops the body entirely — the
  // dock is just its header bar until expanded.
  const visRows = open ? settings.bigRows : settings.smallRows;
  const wantH = visRows * ROW_H;
  // Never below one row: a cramped dock still beats an invisible one.
  const bodyH = Math.max(ROW_H, Math.min(wantH, room));

  const placedStyle: React.CSSProperties | undefined = placed
    ? dock.anchor === "bottom"
      ? { left: dock.x!, bottom: dock.y!, top: "auto", right: "auto" }
      : { left: dock.x!, top: dock.y!, bottom: "auto", right: "auto" }
    : undefined;

  return (
    <div
      ref={panelRef}
      style={placedStyle}
      className={cn(
        "fixed z-40 hidden md:flex w-[290px] flex-col bg-panel border border-border rounded-lg shadow-xl overflow-hidden",
        !placed && "bottom-3 right-3",
      )}
    >
      <div
        onPointerDown={onHeaderPointerDown}
        title="Drag to move"
        className="flex items-center gap-1 pr-2 bg-bg-elev-1/50 border-b border-border cursor-grab active:cursor-grabbing touch-none select-none"
      >
        <button
          type="button"
          onClick={toggle}
          className="flex items-center gap-2 flex-1 min-w-0 px-3 py-2 hover:bg-bg-elev-1 text-left"
          title={open ? "Collapse" : "Expand"}
          aria-expanded={open}
        >
          {/* ">" collapsed, "⌄" expanded — the marker points where a click
           *  takes you. */}
          <span className="text-[11px] text-fg-dim w-3 shrink-0" aria-hidden>
            {open ? "⌄" : ">"}
          </span>
          <span className="text-[12px] font-medium text-fg flex items-center gap-1.5 truncate">
            <span aria-hidden>💰</span> Buys &amp; tips
            {/* Not-yet-seen sales. Green when there's something to look at;
             *  a dim 0 otherwise, so the number is always there to glance. */}
            <span
              className={cn(
                "shrink-0 px-1 min-w-[16px] h-[14px] rounded-full text-[9px] font-semibold grid place-items-center",
                unseen > 0
                  ? "bg-ok text-white"
                  : "bg-bg-elev-1 text-fg-dim border border-border",
              )}
              title={
                unseen > 0
                  ? `${unseen} new sale${unseen === 1 ? "" : "s"} since you last looked`
                  : "No new sales since you last looked"
              }
            >
              {unseen}
            </span>
            {anyRefreshing && (
              <span className="text-fg-dim animate-pulse" title="Refreshing…" aria-hidden>↻</span>
            )}
          </span>
        </button>
        {/* Opens the /group tab on the newest payers. Clicking again while
         *  a group tab is alive RE-SEEDS that same tab with the current
         *  top 8 instead of spawning another. */}
        <button
          type="button"
          onClick={() => {
            // Same drag-swallow as the toggle: a drag released over this
            // button must not also open a group tab.
            if (draggedRef.current) { draggedRef.current = false; return; }
            void openGroupTabWithSlots(groupSlots);
          }}
          disabled={groupSlots.length === 0}
          className="shrink-0 px-1.5 py-1 rounded text-[11px] text-fg-dim hover:text-fg hover:bg-bg-elev-1 disabled:opacity-40 disabled:hover:bg-transparent"
          title={`Open the last ${Math.min(groupSlots.length, GROUP_SLOT_CAP)} payers in the group tab (click again to refresh it)`}
        >
          <span aria-hidden>👥</span> {groupSlots.length}
        </button>
        <button
          type="button"
          onClick={() => {
            if (draggedRef.current) { draggedRef.current = false; return; }
            setSettingsOpen((v) => !v);
          }}
          className={cn(
            "shrink-0 px-1.5 py-1 rounded text-[11px] hover:text-fg hover:bg-bg-elev-1",
            settingsOpen ? "text-fg bg-bg-elev-1" : "text-fg-dim",
          )}
          title="Rail settings"
          aria-expanded={settingsOpen}
        >
          <span aria-hidden>⚙</span>
        </button>
      </div>

      {settingsOpen && (
        <SettingsPanel
          settings={settings}
          accounts={activeAccounts}
          onChange={onSettingsChange}
        />
      )}
      {visRows > 0 && (
        <div
          style={{ maxHeight: bodyH }}
          className="overflow-y-auto overscroll-contain"
        >
          {anyLoading && merged.length === 0 && (
            <div className="px-3 py-2.5 text-[11px] text-fg-dim">Loading…</div>
          )}
          {merged.length === 0 && !anyLoading && (
            <div className="px-3 py-2.5 text-[11px] text-fg-dim">
              No recent purchases or tips.
            </div>
          )}
          {merged.map((m, i) => {
            const fanId = m.n.user?.id ?? m.n.fromUser?.id;
            return (
              <Row
                key={`${m.accountId}:${m.n.id ?? i}`}
                m={m}
                accountLabel={showAccountTag ? (accountLabel.get(m.accountId) ?? null) : null}
                reply={fanId ? (replyStates.get(`${m.accountId}:${fanId}`) ?? null) : null}
                dim={placeholderAids.has(m.accountId)}
              />
            );
          })}
        </div>
      )}
    </div>
  );
}

/** "Jim has purchased your message for $25.22!" → "$25.22". */
function amountFrom(text: string): string | null {
  const m = text.match(/\$\s?([\d,]+(?:\.\d{1,2})?)/);
  return m ? `$${m[1]}` : null;
}

/** What was paid for — tips by typeKey, purchases split post/message/stream
 *  by the notification wording. */
function kindFrom(typeKey: NotifTypeKey, text: string): string {
  if (typeKey === "tip") return "tip";
  if (/\bpost\b/i.test(text)) return "post";
  if (/\bstream\b/i.test(text)) return "stream";
  return "message";
}

function Row({
  m,
  accountLabel,
  reply,
  dim = false,
}: {
  m: MergedItem;
  accountLabel: string | null;
  reply: ReplyState | null;
  /** Last session's snapshot, still being revalidated for this account. */
  dim?: boolean;
}) {
  const { n, accountId, typeKey } = m;
  const user = n.user || n.fromUser || {};
  const avatar = proxyImage(user.avatar ?? null, accountId);
  const text = renderText(n.text || n.description || "", n.replacePairs);
  const amount = amountFrom(text);
  const kind = kindFrom(typeKey, text);
  const name = user.name || user.username || "?";
  const base = user.id
    ? `/chat/${encodeURIComponent(accountId)}/${user.id}`
    : user.username
      ? `/chat/${encodeURIComponent(accountId)}/u/${encodeURIComponent(user.username)}`
      : null;
  const href = base ? `${base}${base.includes("?") ? "&" : "?"}refresh=media` : null;
  const body = (
    <>
      <div className="w-7 h-7 rounded-full bg-bg-elev-1 overflow-hidden shrink-0 grid place-items-center relative">
        {avatar ? (
          <img src={avatar} alt="" loading="lazy" decoding="async" className="w-full h-full object-cover" />
        ) : (
          <span className="text-[10px] text-fg-dim">{name.slice(0, 1).toUpperCase()}</span>
        )}
        <span className="absolute -bottom-1 -right-1 text-[10px]" aria-hidden>
          {typeKey === "tip" ? "💸" : "💰"}
        </span>
      </div>
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2 leading-snug">
          <span className="text-[12px] font-medium text-fg truncate flex-1">{name}</span>
          {amount && (
            <span className="text-[12px] font-semibold text-ok shrink-0">{amount}</span>
          )}
        </div>
        <div className="text-[10px] text-fg-dim mt-0.5 flex items-center gap-1.5">
          <span>{kind}</span>
          <span>·</span>
          <span>{fmtTime(n.createdAt)}</span>
          {accountLabel && (
            <>
              <span>·</span>
              <span className="px-1 rounded bg-bg-elev-1 border border-border">{accountLabel}</span>
            </>
          )}
        </div>
        {reply && <ReplyLine reply={reply} />}
      </div>
    </>
  );
  const cls = cn(
    "px-3 py-2 border-b border-border/60 last:border-b-0 flex items-start gap-2.5 hover:bg-bg-elev-1/40",
    dim && "opacity-60",
  );
  if (href) {
    return (
      <a
        href={href}
        target={chatTabName(href)}
        className={cls}
        title={text}
        onClick={(e) => openChatTab(e, href)}
      >
        {body}
      </a>
    );
  }
  return <div className={cls} title={text}>{body}</div>;
}

/** Labels for the "Show on" surface checkboxes — keyed by the canonical
 *  Surface union, so a new surface in SURFACE_VALUES is a compile error here
 *  until it gets a label (and its checkbox appears for free). */
const SURFACE_LABELS: Record<Surface, string> = {
  inbox: "Inbox",
  popup: "Solo chat",
  group: "Group tab",
  other: "Other pages",
};
const SURFACE_CHOICES = SURFACE_VALUES.map((key) => ({ key, label: SURFACE_LABELS[key] }));

/** The ⚙ popover: where the rail shows at all, how many rows each size
 *  shows, and which models feed it. Writes straight through on every change
 *  — no Save button, the rail re-renders under you so the effect IS the
 *  feedback. */
function SettingsPanel({
  settings,
  accounts,
  onChange,
}: {
  settings: RailSettings;
  accounts: readonly { id: string; nickname?: string | null }[];
  onChange: (s: RailSettings) => void;
}) {
  // null (= all) is rendered as every box ticked, so "all" and "everything
  // picked" look the same — which is what a user means by them anyway.
  const picked = settings.accounts;
  const isOn = (id: string) => (picked ? picked.includes(id) : true);

  const toggleAccount = (id: string) => {
    const current = picked ?? accounts.map((a) => a.id);
    const next = current.includes(id)
      ? current.filter((x) => x !== id)
      : [...current, id];
    // Unticking the last model would blank the rail forever — read that as
    // "back to all" rather than leaving a dead dock on screen.
    if (next.length === 0 || next.length === accounts.length) {
      onChange({ ...settings, accounts: null });
      return;
    }
    onChange({ ...settings, accounts: next });
  };

  const surfaceOn = (s: Surface) => railVisibleOn(settings, s);
  const toggleSurface = (s: Surface) => {
    const current = settings.surfaces ?? SURFACE_CHOICES.map((c) => c.key);
    const next = current.includes(s)
      ? current.filter((x) => x !== s)
      : [...current, s];
    // parseSurfaces applies the same guard on read, but normalise here too so
    // the ticks reflect what will actually happen: all off = back to all.
    onChange({ ...settings, surfaces: parseSurfaces(next) });
  };

  return (
    <div className="px-3 py-2.5 border-b border-border bg-bg-elev-1/30 text-[11px] space-y-2.5">
      <div>
        <span className="text-fg-dim">Show on</span>
        <div className="flex items-center flex-wrap gap-x-3 gap-y-1 mt-1">
          {SURFACE_CHOICES.map((s) => (
            <label
              key={s.key}
              className="flex items-center gap-1.5 cursor-pointer hover:text-fg text-fg-dim"
            >
              <input
                type="checkbox"
                checked={surfaceOn(s.key)}
                onChange={() => toggleSurface(s.key)}
                className="accent-info"
              />
              <span>{s.label}</span>
            </label>
          ))}
        </div>
        <p className="text-[10px] text-fg-dim mt-1 leading-snug">
          Unticking the surface you&apos;re on hides the rail here instantly —
          bring it back from the 🔔 dropdown.
        </p>
      </div>

      <div className="flex items-center justify-between gap-2">
        <label htmlFor="rail-small" className="text-fg-dim">Rows when small</label>
        <select
          id="rail-small"
          value={settings.smallRows}
          onChange={(e) => onChange({ ...settings, smallRows: Number(e.target.value) })}
          className="bg-panel border border-border rounded px-1.5 py-0.5 text-fg"
        >
          {SMALL_ROW_CHOICES.map((n) => (
            <option key={n} value={n}>{n === 0 ? "0 (bar only)" : n}</option>
          ))}
        </select>
      </div>

      <div className="flex items-center justify-between gap-2">
        <label htmlFor="rail-big" className="text-fg-dim">Rows when big</label>
        <select
          id="rail-big"
          value={settings.bigRows}
          onChange={(e) => onChange({ ...settings, bigRows: Number(e.target.value) })}
          className="bg-panel border border-border rounded px-1.5 py-0.5 text-fg"
        >
          {BIG_ROW_CHOICES.map((n) => <option key={n} value={n}>{n}</option>)}
        </select>
      </div>

      <div>
        <div className="flex items-center justify-between gap-2 mb-1">
          <span className="text-fg-dim">Models</span>
          <span className="text-[10px] text-fg-dim">
            {picked ? `${picked.length}/${accounts.length}` : `all ${accounts.length}`}
          </span>
        </div>
        <div className="max-h-[104px] overflow-y-auto space-y-0.5 pr-1">
          {accounts.map((a) => (
            <label
              key={a.id}
              className="flex items-center gap-2 py-0.5 cursor-pointer hover:text-fg text-fg-dim"
            >
              <input
                type="checkbox"
                checked={isOn(a.id)}
                onChange={() => toggleAccount(a.id)}
                className="accent-info"
              />
              <span className="truncate">{a.nickname || a.id}</span>
            </label>
          ))}
        </div>
        <p className="text-[10px] text-fg-dim mt-1 leading-snug">
          Independent of the model you&apos;re scoped to — the rail watches all
          models unless you narrow it here.
        </p>
      </div>
    </div>
  );
}

/** Last message in the thread + where it stands.
 *
 *  The orange dot means "this thread still wants a HUMAN" — it shows when the
 *  fan spoke last, and stays lit when the only thing that answered them was an
 *  automation. It clears the moment a chatter actually replies. Blue keeps its
 *  inbox meaning: unread from the fan, or (as ✓✓) our message opened by them.
 *    blue badge  — the fan spoke last and we haven't read it.
 *    orange dot  — nobody human has answered them.
 *    ✓✓ blue     — a chatter spoke last and the fan has opened it.
 *    ✓ grey      — a chatter spoke last, delivered, not opened yet. */
function ReplyLine({ reply }: { reply: ReplyState }) {
  const { text, outbound, seen, unread, byBot } = reply;
  const tick = (
    <span
      className={cn("shrink-0", seen ? "text-info" : "text-fg-dim")}
      title={seen ? "Seen by the fan" : "Delivered — not opened yet"}
    >
      {seen ? "✓✓" : "✓"}
    </span>
  );
  return (
    <div className="text-[10px] mt-0.5 flex items-center gap-1.5">
      <span className="truncate flex-1 text-fg-dim italic" title={text}>
        {outbound ? (byBot ? "ai: " : "you: ") : ""}
        {text || "—"}
      </span>
      {outbound ? (
        <span className="flex items-center gap-1 shrink-0">
          {tick}
          {byBot && (
            <span
              className="w-2 h-2 rounded-full bg-warn"
              title="Only the AI has answered — no human reply yet"
            />
          )}
        </span>
      ) : unread ? (
        <span
          className="shrink-0 px-1 h-[14px] rounded-full bg-info text-white text-[9px] font-semibold grid place-items-center"
          title="They messaged you — unread"
        >
          new
        </span>
      ) : (
        <span
          className="shrink-0 w-2 h-2 rounded-full bg-warn"
          title="Read · awaiting your reply"
        />
      )}
    </div>
  );
}

function renderText(raw: string, replacePairs?: Record<string, string>): string {
  let s = raw;
  if (replacePairs) {
    for (const [k, v] of Object.entries(replacePairs)) s = s.split(k).join(v);
  }
  return s.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();
}

function fmtTime(iso?: string): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const ms = Date.now() - d.getTime();
  const sec = Math.floor(ms / 1000);
  if (sec < 60) return `${sec}s ago`;
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min}m ago`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr}h ago`;
  const day = Math.floor(hr / 24);
  if (day < 30) return `${day}d ago`;
  return d.toLocaleDateString([], { month: "short", day: "numeric" });
}
