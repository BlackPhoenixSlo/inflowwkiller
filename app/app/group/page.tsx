"use client";

/**
 * /group — group-chat tab. Up to 8 individual fan conversations rendered
 * side-by-side for fast multi-chat across accounts/models.
 *
 * Not a real group thread (OF has no such concept) — just stacked panes,
 * each posting to its own one-on-one chat.
 *
 * Per-tab identity: each /group tab generates a tabId (persisted in
 * sessionStorage so a refresh keeps it) and publishes its current state
 * under `chatterly:group-tab:<tabId>` in localStorage with a heartbeat.
 * Source tabs scan all live registries to route adds — clicking 👥 group
 * on a fan already in some tab focuses that specific tab; otherwise the
 * fan goes into the freshest tab with room; if all live tabs are full,
 * a fresh group tab is spawned. Net effect: no accidental duplicates of
 * the same fan across multiple group tabs.
 *
 * BroadcastChannel messages carry the target tabId so only the addressed
 * tab acts on add/focus.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { useQueries, useQueryClient } from "@tanstack/react-query";

import { ChatList, type ChatListSelection } from "@/components/chat/ChatList";
import { GroupPane } from "@/components/group/GroupPane";
import { useDocumentTitle } from "@/hooks/useDocumentTitle";
import { useInboxRealtime } from "@/hooks/useInboxRealtime";
import { cn } from "@/lib/utils";
import { proxyImage, relay, type OFChatItem, type OFUserMini } from "@/lib/relay";
import {
  GROUP_CHANNEL_NAME,
  GROUP_HEARTBEAT_MS,
  GROUP_HEARTBEAT_TTL_MS,
  GROUP_REPLACE_EVENT,
  GROUP_SLOT_CAP,
  GROUP_WINDOW_NAME,
  parseSlotsParam,
  readAllLiveGroupTabs,
  removeGroupTabRegistry,
  writeGroupTabRegistry,
  type GroupChannelMessage,
  type GroupSlot,
} from "@/lib/groupChannel";

const STORAGE_KEY = "chatterly:group-slots";
const TAB_ID_KEY = "chatterly:group-tab-id";

type Slot = GroupSlot;

function readSlots(): Slot[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = window.sessionStorage.getItem(STORAGE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw) as unknown;
    if (!Array.isArray(parsed)) return [];
    const out: Slot[] = [];
    for (const item of parsed) {
      if (!item || typeof item !== "object") continue;
      const acct = (item as { accountId?: unknown }).accountId;
      const fan = (item as { fanId?: unknown }).fanId;
      if (typeof acct !== "string" || typeof fan !== "number") continue;
      if (!Number.isFinite(fan)) continue;
      out.push({ accountId: acct, fanId: fan });
    }
    return out.slice(0, GROUP_SLOT_CAP);
  } catch {
    return [];
  }
}

function writeSlots(slots: Slot[]): void {
  try {
    window.sessionStorage.setItem(STORAGE_KEY, JSON.stringify(slots));
  } catch {
    /* quota / safari private — accept the loss */
  }
}

/** Parse the seed params once and clear them from the URL so a refresh
 *  doesn't re-seed or re-claim:
 *    ?add=<acctId>:<fanId>              — one fan (👥 from a chat header)
 *    ?slots=<acctId>:<fanId>,...        — a whole list (money rail's last-8)
 *    ?spawn=<token>                     — pairs us with the source tab
 *  Returns the seeded slots (empty when this is a plain reload). */
function takeSeedParams(): { seeds: Slot[]; spawnToken: string | null } {
  if (typeof window === "undefined") return { seeds: [], spawnToken: null };
  const url = new URL(window.location.href);
  const rawAdd = url.searchParams.get("add");
  const rawSlots = url.searchParams.get("slots");
  const spawnToken = url.searchParams.get("spawn");
  if (!rawAdd && !rawSlots && !spawnToken) return { seeds: [], spawnToken: null };
  url.searchParams.delete("add");
  url.searchParams.delete("slots");
  url.searchParams.delete("spawn");
  // replaceState — we don't want these to live in history.
  window.history.replaceState({}, "", url.pathname + (url.search ? url.search : "") + url.hash);
  if (rawSlots) return { seeds: parseSlotsParam(rawSlots), spawnToken };
  if (!rawAdd) return { seeds: [], spawnToken };
  const idx = rawAdd.lastIndexOf(":");
  if (idx <= 0) return { seeds: [], spawnToken };
  const accountId = rawAdd.slice(0, idx);
  const fan = Number(rawAdd.slice(idx + 1));
  if (!accountId || !Number.isFinite(fan) || fan <= 0) return { seeds: [], spawnToken };
  return { seeds: [{ accountId, fanId: fan }], spawnToken };
}

/** Stable per-tab id. Re-uses an existing one from sessionStorage so a
 *  refresh doesn't rotate identity (which would orphan the registry
 *  entry for TTL before scanners cleaned up). */
function resolveTabId(): string {
  if (typeof window === "undefined") return "";
  try {
    const existing = window.sessionStorage.getItem(TAB_ID_KEY);
    if (existing) return existing;
    const cryptoObj = window.crypto as Crypto & { randomUUID?: () => string };
    const id = cryptoObj.randomUUID
      ? cryptoObj.randomUUID()
      : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
    window.sessionStorage.setItem(TAB_ID_KEY, id);
    return id;
  } catch {
    return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
  }
}

export default function GroupChatPage() {
  // Keep SSE patches landing in the right query caches — same hook the
  // inbox page mounts. Without it the panes only pick up new messages
  // via the 30s poll.
  useInboxRealtime();

  // Hydration-gated: SSR renders nothing, the post-mount effect seeds
  // from sessionStorage + ?add=. Avoids a flicker of empty grid.
  const [slots, setSlots] = useState<Slot[]>([]);
  const [hydrated, setHydrated] = useState(false);
  const tabIdRef = useRef<string>("");
  const createdAtRef = useRef<number>(0);
  const spawnTokenRef = useRef<string | null>(null);
  const lastTickAtRef = useRef<number>(0);

  // Keep a ref to current slots so the heartbeat interval can publish
  // them without needing to re-bind every change. State drives React;
  // the ref just exists for cross-effect reads.
  const slotsRef = useRef<Slot[]>([]);
  useEffect(() => { slotsRef.current = slots; }, [slots]);

  useEffect(() => {
    tabIdRef.current = resolveTabId();
    createdAtRef.current = Date.now();
    const { seeds, spawnToken } = takeSeedParams();
    spawnTokenRef.current = spawnToken;
    // ?add= / ?slots= is the marker that this is a FRESHLY-OPENED group
    // tab (someone clicked 👥 group on a chat, or "open last 8" on the
    // money rail — the cold path or the overflow path). In every case
    // the new tab should start clean with just the requested fans, even
    // though some browsers copy sessionStorage to a window.open'd child
    // (which would otherwise carry over the previous group session).
    //
    // Neither param means this is a reload of the SAME tab (or someone
    // typed /group manually) — restore the slot list from
    // sessionStorage so refresh keeps the layout.
    let initial: Slot[] = seeds.length > 0 ? seeds : readSlots();
    // Reconcile-on-mount: drop slots that an OLDER live tab already
    // holds. Handles the residual case where two source tabs raced
    // to spawn and we both ended up with the same fan. Older wins.
    const others = readAllLiveGroupTabs().filter((t) => t.tabId !== tabIdRef.current);
    if (others.length > 0) {
      initial = initial.filter((s) => {
        const olderOwner = others.find((t) =>
          t.createdAt < createdAtRef.current &&
          t.slots.some((x) => x.accountId === s.accountId && x.fanId === s.fanId),
        );
        return !olderOwner;
      });
    }
    setSlots(initial);
    writeSlots(initial);
    setHydrated(true);
  }, []);

  // Persist on every change. Also write the per-tab registry entry so
  // source tabs see the updated slot list immediately (heartbeat tick
  // also writes, but that has up to GROUP_HEARTBEAT_MS lag).
  useEffect(() => {
    if (!hydrated) return;
    writeSlots(slots);
    if (tabIdRef.current) writeGroupTabRegistry(tabIdRef.current, createdAtRef.current, slots);
  }, [slots, hydrated]);

  const addSlot = useCallback((sel: ChatListSelection) => {
    setSlots((cur) => {
      if (cur.some((s) => s.accountId === sel.accountId && s.fanId === sel.fanId)) {
        return cur;
      }
      if (cur.length >= GROUP_SLOT_CAP) return cur;
      return [...cur, { accountId: sel.accountId, fanId: sel.fanId }];
    });
  }, []);

  // Same-tab re-seed. The money rail lives in the root layout, so it also
  // renders HERE — and a BroadcastChannel never delivers to its own sender,
  // so clicking "open last 8" inside the group tab reaches us as a DOM
  // event instead of a `replace` message.
  useEffect(() => {
    const onReplace = (e: Event) => {
      const next = (e as CustomEvent<{ slots?: Slot[] }>).detail?.slots;
      if (!Array.isArray(next) || next.length === 0) return;
      setSlots(next.slice(0, GROUP_SLOT_CAP));
    };
    window.addEventListener(GROUP_REPLACE_EVENT, onReplace);
    return () => window.removeEventListener(GROUP_REPLACE_EVENT, onReplace);
  }, []);

  // Heartbeat (registry refresh) + BroadcastChannel listener. The
  // registry is keyed by this tab's id, so multiple group tabs each
  // have their own entry — last-writer-wins doesn't apply, scanners
  // see all of them.
  useEffect(() => {
    if (!hydrated) return;
    // Stamp window.name so a same-BCG window.open(..., GROUP_WINDOW_NAME)
    // call can find this tab. Cross-BCG it doesn't — focus relies on
    // the broadcast path for those cases. Cleared on unmount so a
    // client-side navigation away from /group doesn't leave the stamp
    // behind — `openGroupTab` keys off window.name as a re-entry guard
    // and would otherwise no-op every 👥 click on the next page. Don't
    // try to restore a prior value: window.name persists across refresh,
    // so the "prior" value is often this same stamp from a past mount.
    try { window.name = GROUP_WINDOW_NAME; } catch { /* sealed in some iframes */ }
    const tabId = tabIdRef.current;
    if (!tabId) return;

    const tick = () => {
      writeGroupTabRegistry(tabId, createdAtRef.current, slotsRef.current);
      lastTickAtRef.current = Date.now();
    };
    tick();
    const interval = window.setInterval(tick, GROUP_HEARTBEAT_MS);

    let channel: BroadcastChannel | null = null;
    try {
      channel = new BroadcastChannel(GROUP_CHANNEL_NAME);
      const ch = channel;
      // Announce ourselves so the source that spawned us can pair its
      // held Window ref with our tabId. No-op for the source(s) that
      // didn't spawn us (they ignore claims for tokens they don't hold).
      if (spawnTokenRef.current) {
        try { ch.postMessage({ type: "claim", tabId, spawnToken: spawnTokenRef.current }); } catch {}
      }
      ch.onmessage = (e: MessageEvent<GroupChannelMessage>) => {
        const msg = e.data;
        if (!msg) return;
        // Address filter: ignore messages not meant for THIS tab.
        // Multiple group tabs are alive simultaneously and each scopes
        // its actions to its own tabId.
        if (msg.type !== "add" && msg.type !== "focus" && msg.type !== "replace") return;
        if (msg.tabId !== tabId) return;
        if (msg.type === "replace") {
          // Destructive re-seed from the money rail: this tab becomes the
          // requested list (newest 8 buyers), dropping whatever it held.
          const next = Array.isArray(msg.slots) ? msg.slots.slice(0, GROUP_SLOT_CAP) : [];
          if (next.length > 0) setSlots(next);
          try { ch.postMessage({ type: "add-ack", tabId, reqId: msg.reqId, ok: next.length > 0 }); } catch {}
        }
        if (msg.type === "add") {
          if (typeof msg.accountId !== "string" || typeof msg.fanId !== "number") return;
          let accepted = false;
          setSlots((cur) => {
            if (cur.some((s) => s.accountId === msg.accountId && s.fanId === msg.fanId)) {
              accepted = true;
              return cur;
            }
            if (cur.length >= GROUP_SLOT_CAP) {
              accepted = false;
              return cur;
            }
            accepted = true;
            return [...cur, { accountId: msg.accountId, fanId: msg.fanId }];
          });
          // ACK lets the source decide whether to spawn an overflow tab
          // when we couldn't actually accept (full since the snapshot).
          try { ch.postMessage({ type: "add-ack", tabId, reqId: msg.reqId, ok: accepted }); } catch {}
        }
        // For both add and focus: try to bring this tab forward.
        // Browsers vary on whether window.focus() from a
        // BroadcastChannel handler is honored. When it works the click
        // on the source tab feels like "switched to group". When
        // denied, slot still landed; user alt-tabs manually.
        try { window.focus(); } catch { /* policy denial — ignore */ }
      };
    } catch {
      /* No BroadcastChannel — gracefully degrade. The source tab's
         fallback path (window.open with ?add=) still works. */
    }

    // Frozen-tab reconcile: if this tab was background-throttled for
    // longer than the registry TTL, another tab may have spawned and
    // taken on our fans assuming we were dead. On wake, drop any of
    // our slots that ANY other live tab also holds — the user has
    // moved on, the other tab is canonical.
    const onVisibility = () => {
      if (document.visibilityState !== "visible") return;
      const stale = Date.now() - lastTickAtRef.current > GROUP_HEARTBEAT_TTL_MS;
      // Refresh our heartbeat immediately regardless — keeps scanners
      // from treating us as dead during the reconcile pass.
      tick();
      if (!stale) return;
      const others = readAllLiveGroupTabs().filter((t) => t.tabId !== tabId);
      if (others.length === 0) return;
      setSlots((cur) => cur.filter((s) => !others.some((t) =>
        t.slots.some((x) => x.accountId === s.accountId && x.fanId === s.fanId),
      )));
    };
    document.addEventListener("visibilitychange", onVisibility);

    // pagehide is the reliable cross-browser teardown signal (fires on
    // bfcache eviction and iOS where beforeunload is unreliable).
    const onPagehide = () => { removeGroupTabRegistry(tabId); };
    window.addEventListener("pagehide", onPagehide);

    return () => {
      window.clearInterval(interval);
      window.removeEventListener("pagehide", onPagehide);
      document.removeEventListener("visibilitychange", onVisibility);
      removeGroupTabRegistry(tabId);
      if (channel) { try { channel.close(); } catch {} }
      try { if (window.name === GROUP_WINDOW_NAME) window.name = ""; } catch {}
    };
  }, [hydrated]);

  const removeSlot = useCallback((accountId: string, fanId: number) => {
    setSlots((cur) =>
      cur.filter((s) => !(s.accountId === accountId && s.fanId === fanId)),
    );
  }, []);

  // Unresponded count among loaded panes: derived from whatever chats
  // data is already in the React Query cache (no extra fetches). SSE
  // keeps lastMessage current; the cache subscription re-runs this
  // derivation whenever any ["chats", ...] entry changes.
  const qc = useQueryClient();
  const [chatsCacheTick, setChatsCacheTick] = useState(0);
  useEffect(() => {
    // Only react to actual data updates. observerAdded/Removed events
    // fire on every render of subscribers (e.g. ChatList mounted below)
    // and would otherwise cause an infinite render loop.
    const unsub = qc.getQueryCache().subscribe((e) => {
      if (e?.type !== "updated") return;
      const k = e?.query?.queryKey as readonly unknown[] | undefined;
      if (k && k[0] === "chats") setChatsCacheTick((t) => t + 1);
    });
    return unsub;
  }, [qc]);
  const unrespondedCount = useMemo(() => {
    const byKey = new Map<string, OFChatItem>();
    const entries = qc.getQueriesData<{ pages: { rows: OFChatItem[] }[] }>({ queryKey: ["chats"] });
    for (const [, data] of entries) {
      if (!data?.pages) continue;
      for (const p of data.pages) for (const c of p.rows) {
        const k = `${c.__accountId ?? ""}:${c.withUser.id}`;
        if (!byKey.has(k)) byKey.set(k, c);
      }
    }
    let n = 0;
    for (const s of slots) {
      const c = byKey.get(`${s.accountId}:${s.fanId}`);
      const lm = c?.lastMessage;
      if (!lm) continue;
      if (lm.fromUser?.id != null && lm.fromUser.id !== Number(s.accountId)) n++;
    }
    return n;
    // chatsCacheTick is a render trigger so cache patches re-evaluate.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [slots, qc, chatsCacheTick]);

  const groupTabTitle = unrespondedCount > 0
    ? `(${unrespondedCount}) Group chat · Fastt`
    : "Group chat · Fastt";
  useDocumentTitle(groupTabTitle);

  const full = slots.length >= GROUP_SLOT_CAP;
  // Responsive grid (md+): panes shrink as count grows. Tailwind picks
  // the narrowest column that still fits ~280px content. At 8 panes on
  // a 1440 monitor that lands at ~250px each — tight but legible. Mobile
  // (< md) doesn't use the grid at all — see the tab strip below.
  const gridCols =
    slots.length <= 1
      ? "grid-cols-1"
      : slots.length === 2
        ? "grid-cols-2"
        : slots.length <= 4
          ? "grid-cols-2"
          : "grid-cols-2 lg:grid-cols-3 xl:grid-cols-4";

  // Mobile renders one pane at a time, switched via a horizontal tab
  // strip. Reason: 8 simultaneous `useChatMessages` + SSE subscriptions
  // on a phone is brutal — at 360px wide there's no parallel-visibility
  // win to justify the CPU. Tabs let only the active pane stay hot;
  // others unmount via the React key=, freeing their hooks.
  const [activeSlotIdx, setActiveSlotIdx] = useState(0);
  const prevSlotCountRef = useRef(0);
  useEffect(() => {
    const prev = prevSlotCountRef.current;
    prevSlotCountRef.current = slots.length;
    if (slots.length === 0) {
      if (activeSlotIdx !== 0) setActiveSlotIdx(0);
      return;
    }
    if (slots.length > prev) {
      // A slot was just added — jump to it. Matches the user intent of
      // "I clicked this fan in the rail, I want to see them now."
      setActiveSlotIdx(slots.length - 1);
    } else if (activeSlotIdx >= slots.length) {
      // Active slot was removed — clamp to the new last slot.
      setActiveSlotIdx(slots.length - 1);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [slots]);

  // Observe-only cache reads for tab labels — ChatList's enrichWithUsers
  // has already populated `["of-user", aid, fid]` for any slot that came
  // from the rail. queryFn rejects but `enabled: false` keeps it from
  // running; we just want the cached data if present.
  const slotProfiles = useQueries({
    queries: slots.map((s) => ({
      queryKey: ["of-user", s.accountId, s.fanId] as const,
      queryFn: (): Promise<OFUserMini> => Promise.reject(new Error("observe-only")),
      enabled: false,
      staleTime: 24 * 60 * 60 * 1000,
    })),
  });
  const tabLabel = (i: number): string => {
    const s = slots[i];
    if (!s) return "";
    const p = slotProfiles[i]?.data as OFUserMini | undefined;
    return p?.customNickname || p?.name || p?.username || `fan ${s.fanId}`;
  };

  return (
    <div className="h-chat flex flex-col lg:grid lg:grid-cols-[300px_minmax(0,1fr)] overflow-hidden">
      {/* On mobile, the inbox rail collapses behind a toggle so the
       *  panes have room. Tap the bar to slide it open/closed.
       *  `shrink-0` keeps the summary visible even when the panes
       *  wrapper below claims remaining space via `flex-1`. */}
      <details className="lg:hidden shrink-0 border-b border-border bg-panel group">
        <summary className="px-3 py-2 text-xs font-semibold cursor-pointer flex items-center justify-between gap-2 list-none [&::-webkit-details-marker]:hidden">
          <span className="flex items-center gap-1.5">
            <span className="text-fg-dim group-open:rotate-90 transition-transform inline-block">›</span>
            <span>Group chat</span>
            {unrespondedCount > 0 && (
              <span
                className="px-1.5 rounded-full bg-warn/20 text-warn text-[10px] font-medium leading-[16px]"
                title="Loaded panes where the fan sent the last message"
              >
                {unrespondedCount} unresponded
              </span>
            )}
          </span>
          <span className="text-[10px] text-fg-dim shrink-0">
            {slots.length} / {GROUP_SLOT_CAP}
          </span>
        </summary>
        <div className="border-t border-border h-[60vh] flex flex-col">
          <div className="px-3 py-2 border-b border-border text-[11px] text-fg-dim flex items-center justify-between gap-2">
            <Link href="/inbox" className="hover:text-fg underline underline-offset-2">
              ← inbox
            </Link>
            {full && <span className="text-warn">full — close one to add</span>}
          </div>
          <div className="flex-1 min-h-0 overflow-hidden">
            <ChatList
              selected={null}
              onSelect={(sel) => {
                if (full) return;
                addSlot(sel);
              }}
              setTabTitle={false}
            />
          </div>
        </div>
      </details>

      <div className="hidden lg:flex flex-col min-h-0 border-r border-border bg-panel">
        <div className="px-3 py-2 border-b border-border flex items-center justify-between gap-2">
          <div className="text-xs font-semibold flex items-center gap-1.5">
            <span>Group chat</span>
            {unrespondedCount > 0 && (
              <span
                className="px-1.5 rounded-full bg-warn/20 text-warn text-[10px] font-medium leading-[16px]"
                title="Loaded panes where the fan sent the last message"
              >
                {unrespondedCount} unresponded
              </span>
            )}
          </div>
          <div className="text-[10px] text-fg-dim shrink-0">
            {slots.length} / {GROUP_SLOT_CAP}
          </div>
        </div>
        <div className="px-3 py-2 border-b border-border text-[11px] text-fg-dim flex items-center justify-between gap-2">
          <Link href="/inbox" className="hover:text-fg underline underline-offset-2">
            ← inbox
          </Link>
          {full && <span className="text-warn">full — close one to add</span>}
        </div>
        <div className="flex-1 min-h-0 overflow-hidden">
          <ChatList
            selected={null}
            onSelect={(sel) => {
              if (full) return;
              addSlot(sel);
            }}
            setTabTitle={false}
          />
        </div>
      </div>

      {/* `flex-1 min-h-0` claims the remaining vertical space on mobile
       *  (the outer is `flex flex-col` below md). On md+ the outer is a
       *  grid and `flex-1` is inert — the grid template owns layout. */}
      <div className="flex-1 min-h-0 overflow-hidden flex flex-col">
        {/* Mobile slot tabs — horizontal scrollable strip, one chip per
         *  slot. Active chip highlights, tap to switch the active pane
         *  below. Hidden on md+ (the desktop grid shows all panes). */}
        {hydrated && slots.length > 0 && (
          <nav className="lg:hidden flex overflow-x-auto no-scrollbar border-b border-border bg-panel shrink-0">
            {slots.map((s, i) => (
              <button
                key={`${s.accountId}:${s.fanId}`}
                type="button"
                onClick={() => setActiveSlotIdx(i)}
                className={cn(
                  "px-3 py-2 text-xs whitespace-nowrap border-r border-border shrink-0 max-w-[140px] truncate",
                  i === activeSlotIdx
                    ? "bg-bg text-fg font-medium"
                    : "text-fg-dim hover:text-fg",
                )}
                title={tabLabel(i)}
              >
                {tabLabel(i)}
              </button>
            ))}
          </nav>
        )}

        <div className="flex-1 min-h-0 overflow-hidden">
          {!hydrated ? (
            <div className="grid place-items-center h-full text-sm text-fg-dim">…</div>
          ) : slots.length === 0 ? (
            <div className="grid place-items-center h-full p-8 text-center">
              <div className="max-w-sm space-y-2">
                <div className="text-sm font-medium">No chats in this group yet.</div>
                <p className="text-xs text-fg-dim">
                  Click any conversation in the left rail to add it. Up to {GROUP_SLOT_CAP}
                  {" "}panes side-by-side; refresh keeps them — close the tab to clear.
                </p>
              </div>
            </div>
          ) : (
            <>
              {/* Mobile: only the active slot is mounted. Switching tabs
               *  remounts via React key=, which also tears down the
               *  inactive pane's useChatMessages + SSE merge. */}
              <div className="lg:hidden h-full">
                {slots[activeSlotIdx] && (
                  <GroupPane
                    key={`m:${slots[activeSlotIdx].accountId}:${slots[activeSlotIdx].fanId}`}
                    accountId={slots[activeSlotIdx].accountId}
                    fanId={slots[activeSlotIdx].fanId}
                    onClose={() => removeSlot(slots[activeSlotIdx].accountId, slots[activeSlotIdx].fanId)}
                  />
                )}
              </div>
              {/* Desktop: all panes in a responsive grid. */}
              <div className={`hidden lg:grid ${gridCols} gap-2 p-2 h-full overflow-auto`}>
                {slots.map((s) => (
                  <GroupPane
                    key={`${s.accountId}:${s.fanId}`}
                    accountId={s.accountId}
                    fanId={s.fanId}
                    onClose={() => removeSlot(s.accountId, s.fanId)}
                  />
                ))}
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
