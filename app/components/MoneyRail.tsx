"use client";

/**
 * MoneyRail — sticky bottom-right dock for money notifications (purchases
 * + tips) so chatters can jump straight to the fans who just paid.
 *
 * It fetches OF's /notifications?type=purchases|tip for each in-scope
 * account on mount — collapsed or not — so the rail is fresh on first page
 * load rather than only after the user expands it. Those rows are merged
 * with the localStorage notif-history mirror (which carries SSE arrivals
 * that OF hasn't listed yet). Uses the SAME ["notif-list", aid, type] query
 * keys as the bell, so the cache is shared and the toaster's invalidation
 * on SSE arrival refreshes this list too.
 *
 * Two states, both persisted in localStorage so a reload / popout comes
 * back exactly how you left it:
 *   • collapsed — compact card showing the last 3 money events.
 *   • expanded  — taller list (~8 rows visible, scroll for up to 50).
 *
 * Sticky by design: it never closes on outside click or Escape — only
 * tapping the header toggles it. Rows deep-link to the fan's chat with
 * the one-shot ?refresh=media flag (same as money toasts) since the fan
 * just moved money and the popout's persisted caches are stale by seconds.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { useQueries, useQueryClient } from "@tanstack/react-query";

import { useScope } from "@/contexts/ScopeContext";
import { useActiveAccounts } from "@/hooks/useAccounts";
import { relay, proxyImage, type OFChatItem } from "@/lib/relay";
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

const MONEY_TYPES = ["purchases", "tip"] as const;

const STATE_KEY = "chatterly:money-rail:v1";
const COLLAPSED_ROWS = 3;
const MAX_ROWS = 50;
/** How many rows deep we fetch chat detail (last text + seen state) while
 *  expanded. One OF roundtrip per fan, so we don't do it for all 50 —
 *  rows past this just render without the status line. */
const DETAIL_ROWS = 12;
/** Floor between two forced re-fetches of the same fan's chat detail. */
const DETAIL_REFRESH_MIN_GAP_MS = 10_000;

function readOpen(): boolean {
  if (typeof window === "undefined") return false;
  try {
    const raw = window.localStorage.getItem(STATE_KEY);
    if (!raw) return false;
    return (JSON.parse(raw) as { open?: boolean }).open === true;
  } catch {
    return false;
  }
}

function writeOpen(open: boolean): void {
  try { window.localStorage.setItem(STATE_KEY, JSON.stringify({ open })); } catch { /* ignore */ }
}

interface NotificationUser {
  id?: number;
  name?: string;
  username?: string;
  avatar?: string;
}
interface NotificationItem {
  id?: number | string;
  type?: string;
  text?: string;
  description?: string;
  createdAt?: string;
  user?: NotificationUser;
  fromUser?: NotificationUser;
  replacePairs?: Record<string, string>;
  [k: string]: unknown;
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
): { messageId: number; automationKind: string | null } | null {
  const entries = Object.entries(resp?.by_msg_id ?? {});
  if (entries.length === 0) return null;
  // limit=1, but don't depend on that — take the highest id.
  let best: { messageId: number; automationKind: string | null } | null = null;
  for (const [id, entry] of entries) {
    const messageId = Number(id);
    if (!Number.isFinite(messageId)) continue;
    if (!best || messageId > best.messageId) {
      best = { messageId, automationKind: (entry as AttributionEntry).automation_kind ?? null };
    }
  }
  return best;
}

function msgTs(m?: { createdAt?: string } | null): number {
  return m?.createdAt ? Date.parse(m.createdAt) || 0 : 0;
}

/** Derive the seen / owes-a-reply state for one fan. Mirrors the inbox's
 *  rule: `lastMessage.fromUser.id !== accountId` means the FAN spoke last
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
  lastOutbound: { messageId: number; automationKind: string | null } | null = null,
): ReplyState | null {
  const dLm = detail?.lastMessage ?? null;
  const cLm = cached?.lastMessage ?? null;
  // Strictly newer, so a tie keeps the trustworthy detail row.
  const cacheIsNewer = !!cLm && msgTs(cLm) > msgTs(dLm);
  const lm = cacheIsNewer ? cLm : (dLm ?? cLm);
  if (!lm) return null;

  // Receipts only move forward, so the higher of the two is the truth.
  const lastRead = Math.max(detail?.lastReadMessageId ?? 0, cached?.lastReadMessageId ?? 0);
  const myId = Number(accountId);
  const outbound = lm.fromUser?.id != null && lm.fromUser.id === myId;
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
    lastOutbound.messageId === lm.id &&
    !!lastOutbound.automationKind;

  return {
    text,
    outbound,
    seen: outbound && !stale && lastRead > 0 && lm.id != null && lastRead >= lm.id,
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

export function MoneyRail() {
  const { accountId } = useScope();
  const activeAccounts = useActiveAccounts();
  const [open, setOpen] = useState<boolean>(() => readOpen());
  // Render nothing until after hydration — the collapsed list comes from
  // localStorage, which the server can't see.
  const [mounted, setMounted] = useState(false);
  useEffect(() => { setMounted(true); }, []);

  const toggle = () => {
    setOpen((v) => {
      writeOpen(!v);
      return !v;
    });
  };

  const targetAccountIds = useMemo<string[]>(
    () => (accountId ? [accountId] : activeAccounts.map((a) => a.id)),
    [accountId, activeAccounts],
  );

  // Re-render whenever the SSE toaster mirrors a new arrival into the
  // localStorage history (same-tab custom event + cross-tab storage event).
  const [historyTick, bumpHistory] = useState(0);
  useEffect(() => {
    const bump = () => bumpHistory((n) => n + 1);
    const onStorage = (e: StorageEvent) => {
      if (e.key === "chatterly:notif-history:v1") bump();
    };
    window.addEventListener("chatterly:notif-history", bump);
    window.addEventListener("storage", onStorage);
    return () => {
      window.removeEventListener("chatterly:notif-history", bump);
      window.removeEventListener("storage", onStorage);
    };
  }, []);

  // OF fan-out — one query per account × money type, enabled only while
  // expanded. Query keys intentionally match the bell's so both share one
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
    })),
  });

  const dataSig = listQueries.map((q) => q.dataUpdatedAt).join(",");

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
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dataSig, queryDefs, targetAccountIds, historyTick]);

  // ── Per-fan chat detail: the last message in the thread + whether the
  // fan has read it. OF's /chats/{fanId} carries `lastMessage` and
  // `lastReadMessageId` (the highest message id the fan marked read) —
  // the same pair the inbox uses for its ✓✓ / awaiting-reply markers.
  // Only for the rows we actually render, deduped by fan.
  const detailTargets = useMemo(() => {
    const rows = merged.slice(0, open ? DETAIL_ROWS : COLLAPSED_ROWS);
    const out: { aid: string; fanId: number }[] = [];
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
  }, [merged, open]);

  const chatQueries = useQueries({
    queries: detailTargets.map(({ aid, fanId }) => ({
      queryKey: ["chat-detail", aid, fanId] as const,
      queryFn: () => relay.get<OFChatItem>(`/api/of/v2/chats/${fanId}`, { accountId: aid }),
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
    for (const [key, st] of replyStates) {
      if (!st.stale) continue;
      const last = refreshedAtRef.current.get(key) ?? 0;
      if (now - last < DETAIL_REFRESH_MIN_GAP_MS) continue;
      refreshedAtRef.current.set(key, now);
      const sep = key.lastIndexOf(":");
      const aid = key.slice(0, sep);
      const fanId = Number(key.slice(sep + 1));
      void qc.invalidateQueries({ queryKey: ["chat-detail", aid, fanId] });
      // The attribution row for a just-sent message is written when OF echoes
      // it, so it lands a beat after the optimistic patch. Refetch it too, or
      // a chatter's reply keeps the bot's `automation_kind` from the previous
      // message and the dot never clears.
      void qc.invalidateQueries({ queryKey: ["money-rail-attrib", aid, fanId] });
    }
  }, [replyStates, qc]);

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

  const visible = open ? merged : merged.slice(0, COLLAPSED_ROWS);

  return (
    <div className="fixed bottom-3 right-3 z-40 hidden md:flex w-[290px] flex-col bg-panel border border-border rounded-lg shadow-xl overflow-hidden">
      <div className="flex items-center gap-1 pr-2 bg-bg-elev-1/50 border-b border-border">
        <button
          type="button"
          onClick={toggle}
          className="flex items-center justify-between gap-2 flex-1 min-w-0 px-3 py-2 hover:bg-bg-elev-1 text-left"
          title={open ? "Collapse" : "Expand"}
          aria-expanded={open}
        >
          <span className="text-[12px] font-medium text-fg flex items-center gap-1.5">
            <span aria-hidden>💰</span> Buys &amp; tips
          </span>
          <span className="text-[11px] text-fg-dim">{open ? "▾ close" : "▴"}</span>
        </button>
        {/* Opens the /group tab on the newest payers. Clicking again while
         *  a group tab is alive RE-SEEDS that same tab with the current
         *  top 8 instead of spawning another. */}
        <button
          type="button"
          onClick={() => { void openGroupTabWithSlots(groupSlots); }}
          disabled={groupSlots.length === 0}
          className="shrink-0 px-1.5 py-1 rounded text-[11px] text-fg-dim hover:text-fg hover:bg-bg-elev-1 disabled:opacity-40 disabled:hover:bg-transparent"
          title={`Open the last ${Math.min(groupSlots.length, GROUP_SLOT_CAP)} payers in the group tab (click again to refresh it)`}
        >
          <span aria-hidden>👥</span> {groupSlots.length}
        </button>
      </div>
      <div className={open ? "max-h-[420px] overflow-y-auto" : "overflow-hidden"}>
        {anyLoading && merged.length === 0 && (
          <div className="px-3 py-2.5 text-[11px] text-fg-dim">Loading…</div>
        )}
        {visible.length === 0 && !anyLoading && (
          <div className="px-3 py-2.5 text-[11px] text-fg-dim">
            No recent purchases or tips.
          </div>
        )}
        {visible.map((m, i) => {
          const fanId = m.n.user?.id ?? m.n.fromUser?.id;
          return (
            <Row
              key={`${m.accountId}:${m.n.id ?? i}`}
              m={m}
              accountLabel={showAccountTag ? (accountLabel.get(m.accountId) ?? null) : null}
              reply={fanId ? (replyStates.get(`${m.accountId}:${fanId}`) ?? null) : null}
            />
          );
        })}
      </div>
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
}: {
  m: MergedItem;
  accountLabel: string | null;
  reply: ReplyState | null;
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
  const cls = "px-3 py-2 border-b border-border/60 last:border-b-0 flex items-start gap-2.5 hover:bg-bg-elev-1/40";
  if (href) {
    return (
      <a href={href} target="_blank" rel="noreferrer" className={cls} title={text}>
        {body}
      </a>
    );
  }
  return <div className={cls} title={text}>{body}</div>;
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
