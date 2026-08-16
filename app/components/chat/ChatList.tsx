"use client";

/**
 * ChatList — the left rail of /inbox.
 *
 * Rows display fan avatar, name, last message preview, unread dot, and
 * (in Unified mode) a colored dot identifying the model account. The
 * dot color comes from AccountMeta.color, which Setup lets the user set.
 *
 * Click a row → notify parent with (accountId, fanId). Parent owns the
 * "which chat is selected" state because that state also drives the
 * right-pane MessageList.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQueries, useQueryClient } from "@tanstack/react-query";

import { useChatList } from "@/hooks/useChatList";
import { fetchAllVaultLists } from "@/hooks/useVaultMedia";
import { useRecentChatsLocal } from "@/hooks/useRecentChatsLocal";
import { useActiveAccounts } from "@/hooks/useAccounts";
import { useDocumentTitle } from "@/hooks/useDocumentTitle";
import { useFanSpend } from "@/hooks/useFanSpend";
import { useFanDrawerDefault } from "@/hooks/useFanDrawerDefault";
import { useBlurMode } from "@/hooks/useBlurMode";
import { useCompactMedia } from "@/hooks/useCompactMedia";
import {
  useChatSortMode,
  hybridScore,
  HALF_LIFE_IDLE,
  HALF_LIFE_UNRESPONDED,
  HALF_LIFE_UNREAD,
  type ChatSortMode,
} from "@/hooks/useChatSortMode";
import { useDeferredMount } from "@/hooks/useDeferredMount";
import { useFanActivity } from "@/hooks/useLastPurchases";
import {
  useAllChatFolders,
  useChatFolders,
  usePinChatFolder,
} from "@/hooks/useChatFolders";
import { useScope } from "@/contexts/ScopeContext";
import { cn, decodeHtmlEntities, fmtRelTime, interpretSubStatus } from "@/lib/utils";
import { describeLoadError, proxyImage, relay, type OFChatItem, type OFMessage, type OFMessagesResp, type OFUserMini } from "@/lib/relay";

/** Built-in OF chat filters — most are server-side filters supported
 *  by /chats. `owe-reply` is CLIENT-SIDE only: there's no OF filter for
 *  "fan sent last and we haven't replied," so we fetch ALL and filter
 *  locally. Auto-paginates until OWE_REPLY_TARGET rows are filled or
 *  OWE_REPLY_SCAN_CAP rows have been scanned (whichever comes first).
 */
type BuiltinFilter = "all" | "unread" | "pinned" | "priority" | "owe-reply";

const OWE_REPLY_TARGET = 8;       // stop early once we have this many on screen
const OWE_REPLY_SCAN_CAP = 100;   // and never scan more than this many chats deep

/** Active selection: either a built-in filter or a custom folder by id. */
type ActiveChip =
  | { kind: "builtin"; key: BuiltinFilter }
  | { kind: "folder"; listId: string };

// Hard cap on background pagination. Search bypasses (otherwise typo'd
// queries dead-end after 200 rows). "Load more" click extends by another step.
const SCROLL_CAP_STEP = 200;

if (typeof window !== "undefined" && !(window as unknown as { __chatlistBuildLogged?: boolean }).__chatlistBuildLogged) {
  console.info("[ChatList] build chats-scroll-cap-v6 (rt-row-bubble) loaded");
  (window as unknown as { __chatlistBuildLogged?: boolean }).__chatlistBuildLogged = true;
}

export interface ChatListSelection {
  accountId: string;
  fanId: number;
  chat: OFChatItem;
}

export function ChatList({
  selected, onSelect, setTabTitle = true,
}: {
  selected: ChatListSelection | null;
  onSelect: (sel: ChatListSelection) => void;
  /** When true (default) ChatList writes "(N) Fastt" into document.title
   *  based on the All-set attention count. Pages that want a different
   *  tab-title format (e.g. /group) opt out and set their own. */
  setTabTitle?: boolean;
}) {
  const { scope } = useScope();
  const accounts = useActiveAccounts();
  const qc = useQueryClient();
  const [active, setActive] = useState<ActiveChip>({ kind: "builtin", key: "all" });
  const [query, setQuery] = useState("");
  const [markingAll, setMarkingAll] = useState(false);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const settingsRef = useRef<HTMLDivElement | null>(null);
  const [keepDrawerOpen, setKeepDrawerOpen] = useFanDrawerDefault();
  const [blurMode, setBlurMode] = useBlurMode();
  const [compactMedia, setCompactMedia] = useCompactMedia();
  const [sortMode, setSortMode] = useChatSortMode();
  const [capRows, setCapRows] = useState(SCROLL_CAP_STEP);

  // Hover-prefetch for chat messages. When the pointer rests on a row
  // for ~120ms we kick a prefetchQuery against the same key + queryFn
  // shape useChatMessages uses, so by the time the user actually clicks
  // the first page is already in cache and the chat opens instantly.
  // The 120ms dwell prevents brushing past 30 rows from firing 30
  // requests; TanStack also dedupes if the prefetch is still in flight
  // by the time the click lands. loadOlder reads its cursor from the
  // cached data ([useChatMessages.ts:88-89]) so prefetched warm-cache
  // chats still paginate correctly on scroll-up.
  const prefetchTimerRef = useRef<number | null>(null);
  const PREFETCH_DWELL_MS = 120;
  const PREFETCH_PAGE_SIZE = 30;
  const PREFETCH_STALE_MS = 15_000;
  const prefetchMessages = useCallback((aid: string, fid: number) => {
    if (!aid || !fid) return;
    qc.prefetchQuery({
      queryKey: ["messages", aid, fid],
      queryFn: async () => {
        const resp = await relay.get<OFMessagesResp>(
          `/api/of/v2/chats/${fid}/messages?limit=${PREFETCH_PAGE_SIZE}&order=desc`,
          { accountId: aid },
        );
        // Same shape useChatMessages.queryFn returns: reversed so the UI
        // can render oldest-first top-to-bottom without re-reversing.
        return ((resp.list || []) as OFMessage[]).slice().reverse();
      },
      staleTime: PREFETCH_STALE_MS,
    }).catch(() => { /* fire-and-forget */ });
  }, [qc]);
  // Vault prefetch on hover. TanStack treats prefetchQuery as a no-op
  // when the query is still fresh (staleTime), so this is cheap to fire
  // on every hover — if the inbox-wide warmer already populated this
  // account's caches we just hit the in-memory hit and bail. When the
  // user hovers a row faster than the warmer reaches that account, this
  // covers the gap. Tagged background-priority so a concurrent vault
  // open still jumps the lane. Keys mirror useVaultLists/useVaultMedia.
  const prefetchVault = useCallback((aid: string) => {
    if (!aid) return;
    qc.prefetchQuery({
      queryKey: ["vault-lists", aid],
      // Shared paginating fetcher — a bare limit=50 fetch here would poison
      // the picker's cache with a truncated folder list (see fetchAllVaultLists).
      queryFn: () => fetchAllVaultLists({ accountId: aid, priority: "background" }),
      staleTime: 5 * 60 * 1000,
    }).catch(() => {});
    qc.prefetchInfiniteQuery({
      queryKey: ["vault-media", aid, "all", null],
      initialPageParam: 0,
      queryFn: () =>
        relay.get(
          "/api/of/v2/vault/media?limit=24&offset=0&type=all",
          { accountId: aid, priority: "background" },
        ),
      staleTime: 60_000,
    }).catch(() => {});
  }, [qc]);
  const scheduleRowPrefetch = useCallback((aid: string, fid: number) => {
    if (prefetchTimerRef.current != null) window.clearTimeout(prefetchTimerRef.current);
    prefetchTimerRef.current = window.setTimeout(() => {
      prefetchTimerRef.current = null;
      prefetchMessages(aid, fid);
      prefetchVault(aid);
    }, PREFETCH_DWELL_MS);
  }, [prefetchMessages, prefetchVault]);
  const cancelRowPrefetch = useCallback(() => {
    if (prefetchTimerRef.current != null) {
      window.clearTimeout(prefetchTimerRef.current);
      prefetchTimerRef.current = null;
    }
  }, []);
  // Unmount cleanup: a pending prefetch timer would otherwise fire after
  // the component is gone (cheap but pointless network).
  useEffect(() => () => {
    if (prefetchTimerRef.current != null) {
      window.clearTimeout(prefetchTimerRef.current);
      prefetchTimerRef.current = null;
    }
  }, []);

  // Defer non-critical decoration queries (spend, last purchase, folders)
  // so they don't compete with /chats + enrichWithUsers for OF bandwidth
  // and the relay thread pool on cold load. Two tiers so chips don't all
  // pop in at once:
  //   • foldersReady  (~2.5s) — folder list + activity (cheap, local DB)
  //   • spendReady    (~5.5s) — /users/list view=x spend chips
  //                              (heaviest OF call, lands last)
  const foldersReady = useDeferredMount(2500);
  const spendReady = useDeferredMount(5500);

  // Pinned chat-folders (per single account). Unified mode hides them
  // because pinning is per-account on OF's side.
  const singleAcctId = scope.kind === "model" ? scope.accountId : null;
  const pinnedFolders = useChatFolders(foldersReady ? singleAcctId : null).data ?? [];

  // If the active folder chip got unpinned (or we switched into unified
  // scope), fall back to "All" so we don't hold a phantom selection.
  useEffect(() => {
    if (active.kind !== "folder") return;
    const stillPinned = pinnedFolders.some((f) => String(f.id) === active.listId);
    if (!stillPinned) setActive({ kind: "builtin", key: "all" });
  }, [active, pinnedFolders]);

  // Settings dropdown — close on outside click + Esc.
  useEffect(() => {
    if (!settingsOpen) return;
    const onClick = (e: MouseEvent) => {
      if (!settingsRef.current?.contains(e.target as Node)) setSettingsOpen(false);
    };
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setSettingsOpen(false); };
    // pointerdown (not mousedown): iOS only synthesises mousedown for taps on
    // clickable nodes, so tapping the panel background never dismissed this.
    // pointerdown precedes mousedown for mouse input → desktop unchanged.
    document.addEventListener("pointerdown", onClick);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("pointerdown", onClick);
      document.removeEventListener("keydown", onKey);
    };
  }, [settingsOpen]);

  // Server-side filter to send to /chats. The standard built-ins map 1:1
  // (with "all" → null). `owe-reply` is client-side only — we still pull
  // the full list. Folder selections map to filter=null + list_id.
  const serverFilter =
    active.kind === "builtin"
      ? (active.key === "all" || active.key === "owe-reply" ? null : active.key)
      : null;
  const serverListId =
    active.kind === "folder" ? active.listId : null;

  const q = useChatList({
    filter: serverFilter,
    listId: serverListId,
    query: query || null,
  });

  // useChatList returns a fresh object literal (with a fresh `loadMore`
  // arrow) every render, so depending on the whole `q` in effects would
  // tear down + rebuild the IntersectionObserver and re-run the owe-reply /
  // autofill loaders on every re-render (SSE patches, spend/activity
  // arrivals, sort recomputes). Keep a ref to the latest `q` and expose a
  // STABLE loadMore that reads through it, so those effects can depend on
  // the specific primitives they use (q.hasMore / q.isFetchingMore) plus a
  // stable callback instead of the unstable object.
  const qRef = useRef(q);
  qRef.current = q;
  const loadMore = useCallback(() => qRef.current.loadMore(), []);

  const accountById = useMemo(() => {
    const m = new Map<string, { color?: string | null; nickname?: string | null }>();
    for (const a of accounts) m.set(a.id, { color: a.color, nickname: a.nickname });
    return m;
  }, [accounts]);

  const allRows = q.data ?? [];

  // Cold-start placeholder: while the real `/chats` fetch is still in
  // flight and we have no rows yet, render the local SQLite seed
  // (sub-50ms) so the panel has clickable rows immediately. When the
  // real query lands the seed dissolves on the next render — `q.data`
  // takes over because `displayRows` reads from it first. Filter the
  // seed by accountId in single-model scope so we don't briefly flash
  // other accounts' chats. Enabled only while pending so the seed
  // query doesn't keep running once the real list is in cache.
  const seedAccountId = scope.kind === "model" ? scope.accountId : null;
  const seedQ = useRecentChatsLocal({
    enabled: q.isPending,
    limit: 25,
    accountId: seedAccountId,
  });

  // Owe-reply: client-side filter on `lastFromFan`. Same logic as the
  // yellow-dot pill in the row — fan sent the latest message, ball in
  // our court — but extended to include unread chats (which also imply
  // they sent last).
  const oweReplyActive = active.kind === "builtin" && active.key === "owe-reply";
  const isUnresponded = useCallback((c: OFChatItem) => {
    const accountId = c.__accountId ?? (scope.kind === "model" ? scope.accountId : "");
    const myId = Number(accountId);
    const lm = c.lastMessage;
    return !!lm && lm.fromUser?.id != null && lm.fromUser.id !== myId;
  }, [scope]);
  // Creators WE restricted on OF (relay stamps withUser.isRestricted off its
  // durable of_restricted registry) never count as unread/owe-reply — we cut
  // them off on purpose, their threads aren't backlog. Mirrors the server-side
  // roster-badge fold exactly, so chips and badges can't disagree over them.
  const notRestricted = useCallback(
    (c: OFChatItem) => !c.withUser?.isRestricted, []);
  const rows = useMemo(() => {
    if (!oweReplyActive) return allRows;
    return allRows.filter((c) => isUnresponded(c) && notRestricted(c));
  }, [allRows, oweReplyActive, isUnresponded, notRestricted]);

  // True only when we're rendering local-DB seed in place of real data.
  // Used to (a) display seed rows in the list, and (b) prevent Work
  // Unit B's autofill effect from terminating against the seed's
  // visual overflow — otherwise loadMore would never fire even when
  // the real list is empty and `hasMore` is true.
  const isFromSeed = q.isPending && allRows.length === 0 && seedQ.rows.length > 0;
  const displayRows: OFChatItem[] = useMemo(() => {
    if (!isFromSeed) return rows;
    return oweReplyActive ? seedQ.rows.filter(isUnresponded) : seedQ.rows;
  }, [isFromSeed, rows, oweReplyActive, seedQ.rows, isUnresponded]);

  // Chip counts — derived from already-cached data, zero new fetches.
  // useChatList re-renders ChatList whenever SSE patches the chats cache
  // (useInboxRealtime patches EVERY ["chats", ...] cache, including the
  // active one we subscribe to), so reading qc.getQueryData at render
  // time picks up fresh values without an extra subscription.
  const accountKey =
    scope.kind === "model"
      ? scope.accountId
      : accounts.map((a) => a.id).sort().join(",");
  // 25 = useChatList's PAGE_SIZE default; mirror it so the cache key matches.
  const allFilterKey = useMemo(
    () => ["chats", scope.kind, accountKey, null, null, query || null, 25] as const,
    [scope.kind, accountKey, query],
  );
  const allChipBase = useMemo<OFChatItem[]>(() => {
    // When the All filter is the active query, the live rows ARE the all set.
    if (active.kind === "builtin" && active.key === "all") return allRows;
    const cached = qc.getQueryData<{ pages: { rows: OFChatItem[] }[] }>(allFilterKey);
    if (!cached?.pages) return [];
    const out: OFChatItem[] = [];
    const seen = new Set<string>();
    for (const p of cached.pages) for (const c of p.rows) {
      const k = `${c.__accountId ?? ""}:${c.withUser.id}`;
      if (seen.has(k)) continue;
      seen.add(k);
      out.push(c);
    }
    return out;
  }, [active, allRows, qc, allFilterKey]);
  const activeCount = useMemo(
    () => rows.filter((c) => notRestricted(c) && (c.hasUnread || isUnresponded(c))).length,
    [rows, isUnresponded, notRestricted],
  );
  const allCount = useMemo(
    () => allChipBase.filter((c) => notRestricted(c) && (c.hasUnread || isUnresponded(c))).length,
    [allChipBase, isUnresponded, notRestricted],
  );
  const oweReplyCount = useMemo(
    () => allChipBase.filter((c) => notRestricted(c) && isUnresponded(c)).length,
    [allChipBase, isUnresponded, notRestricted],
  );

  const inboxTabTitle = allCount > 0
    ? `(${allCount}) Inbox · Fastt`
    : "Inbox · Fastt";
  useDocumentTitle(inboxTabTitle, setTabTitle);

  // Deferred: pass empty inputs until the staggered ready flags flip,
  // which short-circuits the inner useQueries fan-out and keeps the cold
  // load free of these chip-population calls. Spend uses the later flag
  // (5.5s) because it issues the heaviest OF call (/users/list view=x);
  // activity uses the earlier flag (2.5s) — payouts is cheap and cached
  // forever after first session.
  const spendMap = useFanSpend(spendReady ? rows : []);
  // One transactions feed per active account in scope. Memoized to avoid
  // re-triggering useQueries when row order changes but account set doesn't.
  const purchaseAccountIds = useMemo(() => {
    if (!foldersReady) return [];
    const set = new Set<string>();
    for (const r of rows) if (r.__accountId) set.add(r.__accountId);
    return Array.from(set).sort();
  }, [rows, foldersReady]);
  const activity = useFanActivity(purchaseAccountIds);
  const lastPurchaseMap = activity.lastPurchase;
  const recentSpendMap = activity.recentSpend;

  // Apply sort mode. "recent" returns the server order untouched. "spend"
  // and "hybrid" both rely on the deferred spendMap — until it lands the
  // sort no-ops, then re-orders in place once data arrives. We sort the
  // *display* set rather than the raw paginated `rows` so chip counts
  // and pagination math stay anchored to the canonical OF order; only
  // visual placement reflects the user's choice. Stable sort is required
  // because Array.prototype.sort is stable in all modern engines, so
  // ties (e.g. two $0 fans) preserve OF's lastActivity ordering.
  const spendForRow = useCallback((c: OFChatItem) => {
    const aid = c.__accountId ?? "";
    const key = `${aid}:${c.withUser.id}`;
    const lifetime = spendMap.get(key)?.spend_cents ?? 0;
    const recent = recentSpendMap.get(key) ?? 0;
    return Math.max(lifetime, recent);
  }, [spendMap, recentSpendMap]);
  const sortKey = `${sortMode}|${spendMap.size}|${recentSpendMap.size}`;
  const sortedDisplayRows = useMemo<OFChatItem[]>(() => {
    if (sortMode === "recent") return displayRows;
    const arr = displayRows.slice();
    if (sortMode === "spend") {
      arr.sort((a, b) => spendForRow(b) - spendForRow(a));
      return arr;
    }
    const isUnread = (c: OFChatItem) =>
      c.hasUnread || (c.unreadMessagesCount ?? 0) > 0;
    if (sortMode === "spend-unread") {
      // Three-tier bucket, then spend desc within each:
      //   2 = unread (blue badge)
      //   1 = read but fan sent last (yellow dot — owe reply)
      //   0 = idle (we sent last, or no messages yet)
      const tierOf = (c: OFChatItem): number => {
        if (isUnread(c)) return 2;
        if (isUnresponded(c)) return 1;
        return 0;
      };
      arr.sort((a, b) => {
        const ta = tierOf(a);
        const tb = tierOf(b);
        if (ta !== tb) return tb - ta;
        return spendForRow(b) - spendForRow(a);
      });
      return arr;
    }
    // hybrid — threshold applies to the DECAYED score, not raw spend.
    // So a $48 fan starts well above the $3 cutoff, but after ~5h of
    // silence their score halves down past it and unread rows start
    // winning. Tiered half-lives keep attention-needing chats up
    // longer: idle 1h, owe-reply 2h, unread 3h. Within the low-value
    // bucket, ties between unread rows fall through to raw spend.
    const LOW_SCORE_CENTS = 300;
    const halfLifeFor = (c: OFChatItem): number => {
      if (isUnread(c)) return HALF_LIFE_UNREAD;
      if (isUnresponded(c)) return HALF_LIFE_UNRESPONDED;
      return HALF_LIFE_IDLE;
    };
    arr.sort((a, b) => {
      const aSpend = spendForRow(a);
      const bSpend = spendForRow(b);
      const aLast = a.lastMessage?.createdAt ?? null;
      const bLast = b.lastMessage?.createdAt ?? null;
      const aScore = hybridScore(aSpend, aLast, halfLifeFor(a));
      const bScore = hybridScore(bSpend, bLast, halfLifeFor(b));
      if (aScore < LOW_SCORE_CENTS && bScore < LOW_SCORE_CENTS) {
        const ua = isUnread(a) ? 1 : 0;
        const ub = isUnread(b) ? 1 : 0;
        if (ua !== ub) return ub - ua;
        // Both unread (or both read) at low value: raw spend desc.
        if (aSpend !== bSpend) return bSpend - aSpend;
      }
      return bScore - aScore;
    });
    return arr;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [displayRows, sortMode, sortKey, spendForRow, isUnresponded]);

  // Per-fan profile cache observer. `enrichWithUsers` writes each fan's
  // {name, username, avatar, customNickname} to ["of-user", aid, fid];
  // here we subscribe to those entries without triggering fetches. The
  // chats cache only carries the slim ids/lastMessage shape — names come
  // from this side cache, so refetches / SSE patches on the chats cache
  // can't make the row revert to "fan {id}". Stable keys per fan.
  const profileQueries = useQueries({
    queries: rows.map((r) => ({
      queryKey: ["of-user", r.__accountId, r.withUser.id] as const,
      queryFn: (): Promise<OFUserMini> =>
        Promise.reject(new Error("observe-only")),
      enabled: false,
      staleTime: Infinity,
    })),
  });
  const profileVersion = profileQueries.map((q) => q.dataUpdatedAt).join("|");
  // Row-identity signature: profileQueries[i] is index-aligned with rows[i],
  // so if rows reorder or swap a slot for a fan with the same dataUpdatedAt
  // the memo would otherwise miss the change and serve the old map.
  const rowSig = rows.map((r) => `${r.__accountId ?? ""}:${r.withUser.id}`).join("|");
  const profileMap = useMemo(() => {
    const m = new Map<string, OFUserMini>();
    profileQueries.forEach((q, i) => {
      const r = rows[i];
      if (!r || !q.data) return;
      m.set(`${r.__accountId ?? ""}:${r.withUser.id}`, q.data as OFUserMini);
    });
    return m;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [profileVersion, rowSig]);

  // Per-success optimistic patch: clear the unread badge on this fan's row
  // across every cached ["chats"] view so the UI only shows "read" for chats
  // that actually got marked. Mirrors useInboxRealtime's cache-shape walk
  // (InfiniteData { pages: [{ rows, hasMore }] }); skips caches that don't
  // hold the row. The trailing invalidate reconciles against OF either way.
  const markRowRead = useCallback((accountId: string, fanId: number) => {
    type Page = { rows: OFChatItem[]; hasMore: boolean };
    type Infinite = { pages: Page[]; pageParams: unknown[] };
    qc.getQueryCache().findAll({ queryKey: ["chats"] }).forEach((cq) => {
      const data = cq.state.data as Infinite | undefined;
      if (!data?.pages?.length) return;
      let touched = false;
      const newPages: Page[] = data.pages.map((p) => ({
        ...p,
        rows: p.rows.map((c) => {
          if ((c.__accountId ?? "") !== accountId || c.withUser.id !== fanId) return c;
          if (!c.hasUnread && (c.unreadMessagesCount ?? 0) === 0) return c;
          touched = true;
          return { ...c, hasUnread: false, unreadMessagesCount: 0 };
        }),
      }));
      if (touched) qc.setQueryData(cq.queryKey, { ...data, pages: newPages });
    });
  }, [qc]);

  async function markAllRead() {
    if (markingAll) return;
    const targets = allRows.filter((c) => c.hasUnread);
    if (targets.length === 0) return;
    if (!confirm(`Mark ${targets.length} chat${targets.length === 1 ? "" : "s"} as read?`)) return;
    setMarkingAll(true);
    let failed = 0;
    try {
      // Sequential POSTs — OF rate-limits aggressively when hammered in
      // parallel. Each success optimistically clears that row's badge; the
      // trailing refetch reconciles whatever stuck.
      for (const c of targets) {
        const accountId = c.__accountId;
        if (!accountId) continue;
        try {
          await relay.post(
            `/api/of/v2/chats/${c.withUser.id}/mark-as-read`,
            undefined,
            { accountId },
          );
          markRowRead(accountId, c.withUser.id);
        } catch (err) {
          failed += 1;
          console.warn("[mark-all-read] failed for", c.withUser.id, err);
        }
      }
      qc.invalidateQueries({ queryKey: ["chats"] });
    } finally {
      setMarkingAll(false);
    }
    if (failed > 0) {
      alert(`Marked ${targets.length - failed} of ${targets.length}; ${failed} failed.`);
    }
  }

  // Reset the scroll cap whenever the query context changes (new filter,
  // folder, scope, or search). A fresh list should get a fresh 200-row budget.
  useEffect(() => {
    setCapRows(SCROLL_CAP_STEP);
  }, [active, query, scope.kind, scope.kind === "model" ? scope.accountId : null]);

  // Pre-warm the SW image cache for the top rows so the OF signed-URL
  // refresh round-trip happens once per fan-session rather than each time
  // a row's <img> mounts. `Image()` rides the browser's image pipeline,
  // which the same-origin SW intercepts → cached for later mounts/popouts.
  // Ref-deduped so a re-sort never re-fires the same URL.
  const preWarmedRef = useRef<Set<string>>(new Set());
  useEffect(() => {
    if (typeof window === "undefined") return;
    for (const c of sortedDisplayRows.slice(0, 30)) {
      const aid = c.__accountId ?? null;
      if (!aid) continue;
      const cached = qc.getQueryData<{ avatar?: string | null }>([
        "of-user", aid, c.withUser.id,
      ]);
      const raw = c.withUser.avatar || cached?.avatar || null;
      if (!raw) continue;
      const url = proxyImage(raw, aid);
      if (!url || preWarmedRef.current.has(url)) continue;
      preWarmedRef.current.add(url);
      const img = new Image();
      img.src = url;
    }
  }, [sortedDisplayRows, qc]);

  // True when we've paginated up to the cap with no active search. The
  // budget is measured against the RAW scanned set (allRows), not the
  // filter view — owe-reply only displays a handful of matches but each
  // page still costs a roundtrip, so the cap has to bite either way.
  const capped = !query && allRows.length >= capRows;

  // Owe-reply: keep loading more pages until we either fill the target
  // (5–8 filtered rows on screen) or scan-cap hits (100 raw rows). OF
  // has no native "fan sent last" filter, so we have to walk the chat
  // list ourselves; the cap keeps that walk bounded.
  const oweReplyScanned = oweReplyActive ? allRows.length : 0;
  const oweReplyMatches = oweReplyActive ? rows.length : 0;
  const oweReplyExhausted =
    oweReplyActive &&
    (oweReplyMatches >= OWE_REPLY_TARGET ||
      oweReplyScanned >= OWE_REPLY_SCAN_CAP ||
      !q.hasMore);
  useEffect(() => {
    if (!oweReplyActive) return;
    if (oweReplyExhausted) return;
    if (q.isFetchingMore) return;
    loadMore();
  }, [oweReplyActive, oweReplyExhausted, oweReplyMatches, oweReplyScanned, q.isFetchingMore, loadMore]);

  // Auto-load more pages only after the user has actually scrolled the
  // panel. Without this gate, an empty/short panel keeps the sentinel in
  // view from the start, so the IntersectionObserver chains loadMore()
  // calls before the user has done anything — burning quota and pulling
  // hundreds of /users/list batches for fans they may never see.
  // Page 1 (~25 rows) loads automatically via useInfiniteQuery; further
  // pages only auto-load once the user starts scrolling.
  const sentinelRef = useRef<HTMLDivElement | null>(null);
  const [userScrolled, setUserScrolled] = useState(false);
  // Bounded auto-fill: if first paint doesn't overflow the scroller (small
  // inbox, or unified mode where one account returns few rows), the IO
  // sentinel sits inside the viewport forever but `userScrolled` never
  // flips, so load-more never fires. Counter caps the burst at 10 pages
  // (confirmed sufficient at 80% zoom; 100% zoom users need at least this
  // many to reach overflow). Reset on query-key change.
  const autofillBurstRef = useRef(0);
  useEffect(() => {
    const el = sentinelRef.current;
    const root = el?.parentElement;
    if (!root) return;
    const onScroll = () => {
      if (root.scrollTop > 0) setUserScrolled(true);
    };
    root.addEventListener("scroll", onScroll, { passive: true });
    return () => root.removeEventListener("scroll", onScroll);
  }, []);
  // Reset the "has scrolled" flag whenever query context changes so a
  // fresh list doesn't inherit the prior scroll permission.
  useEffect(() => {
    setUserScrolled(false);
  }, [active, query, scope.kind, scope.kind === "model" ? scope.accountId : null]);

  useEffect(() => {
    const el = sentinelRef.current;
    if (!el) return;
    const io = new IntersectionObserver(
      (entries) => {
        if (!entries.some((e) => e.isIntersecting)) return;
        if (!qRef.current.hasMore || qRef.current.isFetchingMore) return;
        if (capped) return;
        // Owe-reply has its own bounded walk and intentionally pre-scans
        // without waiting for user scroll. Search also bypasses the gate
        // — typing a name implies "keep scanning for matches." Default
        // mode requires an explicit scroll signal before chaining pages.
        if (oweReplyExhausted) return;
        if (!oweReplyActive && !query && !userScrolled) return;
        loadMore();
      },
      // rootMargin: 0 means "only fire when the sentinel is actually in
      // the viewport," not 200px before. Combined with the userScrolled
      // gate above, this confines auto-load to real bottom-touches.
      { root: el.parentElement, rootMargin: "0px 0px" },
    );
    io.observe(el);
    return () => io.disconnect();
  }, [loadMore, capped, oweReplyExhausted, oweReplyActive, userScrolled, query]);

  // Reset the auto-fill burst counter when the query context changes so a
  // fresh list (new scope, filter, folder, or search) gets a fresh budget.
  useEffect(() => {
    autofillBurstRef.current = 0;
  }, [accountKey, serverFilter, serverListId, query]);

  // Auto-fill until the panel overflows. Pairs with the userScrolled gate
  // on the IO observer: once content is taller than the viewport, scroll
  // takes over and this effect stops firing. Hard-capped at 10 bursts so
  // a truly small inbox or a stuck `hasMore` can't burn quota.
  // Gated on `q.isSuccess && !isFromSeed`: while the cold-start seed is
  // visible, the scroller may already overflow with placeholder rows
  // which would falsely terminate the autofill check, OR — worse — the
  // real query could land with hasMore=true and 0 real rows and we'd
  // loop loadMore against an empty result. Re-runs once seed dissolves.
  useEffect(() => {
    if (!q.isSuccess || isFromSeed) return;
    if (!q.hasMore || q.isFetchingMore) return;
    if (capped) return;
    if (oweReplyExhausted) return;
    const scroller = sentinelRef.current?.parentElement;
    if (!scroller) return;
    // Tab not visible or panel not mounted yet — clientHeight will be 0
    // and we'd burst forever. Wait for it to actually layout.
    if (scroller.offsetParent === null || scroller.clientHeight === 0) return;
    const id = requestAnimationFrame(() => {
      if (scroller.scrollHeight > scroller.clientHeight + 80) return;
      if (autofillBurstRef.current >= 10) return;
      autofillBurstRef.current += 1;
      loadMore();
    });
    return () => cancelAnimationFrame(id);
  }, [rows.length, q.hasMore, q.isFetchingMore, q.isSuccess, isFromSeed, capped, oweReplyExhausted, loadMore]);

  return (
    <div className="flex flex-col h-full min-h-0 bg-panel border-r border-border">
      <div className="p-3 border-b border-border space-y-2">
        <div className="flex items-center justify-between gap-2">
          <h2 className="font-semibold text-sm truncate">
            {scope.kind === "all" ? "Unified Inbox" : (accountById.get(scope.accountId)?.nickname || scope.accountId)}
          </h2>
          <div className="flex items-center gap-3 md:gap-2 shrink-0" ref={settingsRef}>
            <span className="text-[11px] text-fg-dim">
              {q.isFetching ? "…" : rows.length}
            </span>
            <button
              type="button"
              onClick={() => {
                const targets =
                  scope.kind === "model" ? [scope.accountId] : accounts.map((a) => a.id);
                for (const aid of targets) {
                  qc.removeQueries({ queryKey: ["vault-media", aid] });
                  qc.removeQueries({ queryKey: ["vault-lists", aid] });
                  qc.removeQueries({ queryKey: ["wall-media", aid] });
                }
                // Use `refresh()` (not `refetch()`) so the next /chats
                // call carries `?refresh=1` and bypasses the Stage C
                // backend `list_chats` cache. Without this, a recently-
                // cached inbox would survive the button press.
                q.refresh();
              }}
              disabled={q.isFetching}
              className="w-9 h-9 md:w-6 md:h-6 grid place-items-center rounded-md text-fg-dim hover:text-fg hover:bg-bg-elev-1 text-sm disabled:opacity-50"
              title="Refresh inbox"
              aria-label="Refresh inbox"
            >
              <span className={cn("inline-block leading-none", q.isFetching && "animate-spin")}>↻</span>
            </button>
            <div className="relative">
              <button
                type="button"
                onClick={() => setSettingsOpen((v) => !v)}
                className="h-9 md:h-6 px-2.5 md:px-1.5 inline-flex items-center gap-1 rounded-md text-fg-dim hover:text-fg hover:bg-bg-elev-1 text-sm"
                title="Inbox settings"
                aria-label="Inbox settings"
              >
                <span className="leading-none">⚙</span>
                <span className="text-[11px]">Settings</span>
              </button>
              {settingsOpen && (
                <div className="absolute right-0 mt-1 w-[230px] bg-panel border border-border rounded-lg shadow-xl z-30 py-1">
                  <button
                    type="button"
                    onClick={() => { setSettingsOpen(false); markAllRead(); }}
                    disabled={markingAll || !allRows.some((c) => c.hasUnread)}
                    className="w-full flex items-center gap-2 px-3 py-1.5 text-xs text-left text-fg hover:bg-bg-elev-1 disabled:opacity-50"
                  >
                    <span className="w-4 text-center">✓</span>
                    <span>{markingAll ? "Marking…" : "Mark all as read"}</span>
                  </button>
                  <label className="w-full flex items-center gap-2 px-3 py-1.5 text-xs text-fg hover:bg-bg-elev-1 cursor-pointer select-none border-t border-border/60">
                    <input
                      type="checkbox"
                      checked={keepDrawerOpen}
                      onChange={(e) => setKeepDrawerOpen(e.target.checked)}
                      className="shrink-0"
                    />
                    <span>Keep fan info panel open</span>
                  </label>
                  {/* Image-blur mode. Two checkboxes for the two non-default
                   *  modes; checking one unchecks the other so the user
                   *  always sees the live state. Applied to chat media tiles
                   *  + vault thumbnails via the useBlurMode hook. */}
                  <label className="w-full hidden md:flex items-center gap-2 px-3 py-1.5 text-xs text-fg hover:bg-bg-elev-1 cursor-pointer select-none border-t border-border/60">
                    <input
                      type="checkbox"
                      checked={blurMode === "hover"}
                      onChange={(e) => setBlurMode(e.target.checked ? "hover" : "off")}
                      className="shrink-0"
                    />
                    <span>Blur images till hover</span>
                  </label>
                  <label className="w-full flex items-center gap-2 px-3 py-1.5 text-xs text-fg hover:bg-bg-elev-1 cursor-pointer select-none">
                    <input
                      type="checkbox"
                      checked={blurMode === "constant"}
                      onChange={(e) => setBlurMode(e.target.checked ? "constant" : "off")}
                      className="shrink-0"
                    />
                    <span>Blur images constant</span>
                  </label>
                  <label className="w-full flex items-center gap-2 px-3 py-1.5 text-xs text-fg hover:bg-bg-elev-1 cursor-pointer select-none border-t border-border/60">
                    <input
                      type="checkbox"
                      checked={compactMedia}
                      onChange={(e) => setCompactMedia(e.target.checked)}
                      className="shrink-0"
                    />
                    <span>Compact media previews</span>
                  </label>
                  <div className="px-3 py-1.5 text-xs text-fg border-t border-border/60 space-y-1">
                    <div className="text-fg-dim text-[10px] uppercase tracking-wide">Sort by</div>
                    {([
                      { key: "recent", label: "Recent activity" },
                      { key: "spend", label: "Amount spent" },
                      { key: "spend-unread", label: "Spend + unread on top", sub: "unread → owe reply → idle" },
                      { key: "hybrid", label: "Spend + recency", sub: "spend × time-decay" },
                    ] as { key: ChatSortMode; label: string; sub?: string }[]).map((opt) => (
                      <label
                        key={opt.key}
                        className="flex items-center gap-2 cursor-pointer select-none"
                        title={
                          opt.key === "hybrid"
                            ? "Spend × decay. Half-life: idle 1h, owe-reply 2h, unread 3h. Below $3 effective, unread wins."
                            : opt.key === "spend-unread"
                              ? "Unread → owe-reply → idle, spend desc within each tier"
                              : undefined
                        }
                      >
                        <input
                          type="radio"
                          name="chat-sort-mode"
                          checked={sortMode === opt.key}
                          onChange={() => setSortMode(opt.key)}
                          className="shrink-0"
                        />
                        {/* Phone-only sub-line: on desktop these semantics
                         *  live in the row's title= tooltip, which touch has
                         *  no way to surface. Hidden at md → desktop row is
                         *  the bare label exactly as before. */}
                        <span className="flex flex-col">
                          <span>{opt.label}</span>
                          {opt.sub && (
                            <span className="md:hidden text-[10px] leading-tight text-fg-dim">{opt.sub}</span>
                          )}
                        </span>
                      </label>
                    ))}
                  </div>
                </div>
              )}
            </div>
          </div>
        </div>
        <input
          type="text"
          placeholder="Search…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          className="w-full bg-bg border border-border rounded-md px-3 py-2.5 text-base md:px-2 md:py-1.5 md:text-xs placeholder:text-muted focus:outline-none focus:border-accent"
        />
        {/* Row 1: built-in OF filters. Always visible, never overflows. */}
        <div className="flex items-center gap-1 text-[11px] flex-wrap">
          <FilterChip
            active={active.kind === "builtin" && active.key === "all"}
            onClick={() => setActive({ kind: "builtin", key: "all" })}
            count={allCount}
          >All</FilterChip>
          <FilterChip
            active={active.kind === "builtin" && active.key === "unread"}
            onClick={() => setActive({ kind: "builtin", key: "unread" })}
            count={active.kind === "builtin" && active.key === "unread" ? activeCount : null}
          >Unread</FilterChip>
          <FilterChip
            active={active.kind === "builtin" && active.key === "pinned"}
            onClick={() => setActive({ kind: "builtin", key: "pinned" })}
            count={active.kind === "builtin" && active.key === "pinned" ? activeCount : null}
          >📌 Pinned</FilterChip>
          <FilterChip
            active={active.kind === "builtin" && active.key === "priority"}
            onClick={() => setActive({ kind: "builtin", key: "priority" })}
            count={active.kind === "builtin" && active.key === "priority" ? activeCount : null}
          >⚡ Priority</FilterChip>
          <FilterChip
            active={active.kind === "builtin" && active.key === "owe-reply"}
            onClick={() => setActive({ kind: "builtin", key: "owe-reply" })}
            count={oweReplyCount}
            title="Chats where the fan sent the last message and we haven't replied"
          >↩ Owe reply</FilterChip>
        </div>
        {/* Row 2: pinned custom folders + the picker. Horizontally scrolls
         *  when there are too many to fit — they're optional/secondary. */}
        {(pinnedFolders.length > 0 || scope.kind === "model") && (
          <div className="flex items-center gap-1">
            {/* The scroller keeps flex-1 min-w-0 so it still measures exactly
             *  100% of the row (the phone-only ✎ below is display:none at md
             *  and contributes no flex gap), i.e. the desktop strip is
             *  unchanged. */}
            <div className="flex-1 min-w-0 flex items-center gap-1 text-[11px] overflow-x-auto no-scrollbar">
            {pinnedFolders.map((f) => {
              const id = String(f.id);
              const isActive = active.kind === "folder" && active.listId === id;
              return (
                <FilterChip
                  key={id}
                  active={isActive}
                  onClick={() => setActive({ kind: "folder", listId: id })}
                  title={f.name || `List #${id}`}
                  count={isActive ? activeCount : null}
                >
                  📂 {f.name || `List #${id}`}
                </FilterChip>
              );
            })}
            {scope.kind === "model" && (
              <button
                type="button"
                onClick={() => setPickerOpen(true)}
                className="hidden md:inline-block px-2 py-0.5 rounded-full border border-border text-fg-dim hover:text-fg hover:border-border-light transition-colors shrink-0"
                title="Pick folders to pin as chips"
              >
                ✎
              </button>
            )}
            </div>
            {/* Phone twin of the ✎ above: inside the scroller it scrolls out
             *  of reach once a few folders are pinned. Hidden at md, where the
             *  in-strip original is restored by `md:inline-block`. */}
            {scope.kind === "model" && (
              <button
                type="button"
                onClick={() => setPickerOpen(true)}
                className="md:hidden shrink-0 inline-flex items-center px-3 py-1.5 min-h-[36px] rounded-full border border-border text-[11px] text-fg-dim hover:text-fg hover:border-border-light transition-colors"
                title="Pick folders to pin as chips"
                aria-label="Pick folders to pin as chips"
              >
                ✎
              </button>
            )}
          </div>
        )}
      </div>
      {pickerOpen && singleAcctId && (
        <FolderPicker
          accountId={singleAcctId}
          onClose={() => setPickerOpen(false)}
        />
      )}

      <div
        className={cn(
          "flex-1 min-h-0 overflow-y-auto transition-opacity",
          q.isFetching && q.isPlaceholderData && "opacity-60",
        )}
      >
        {q.isError && (
          <div className="p-4 text-xs text-err">
            {describeLoadError(q.error as Error)}
          </div>
        )}
        {!q.isError && rows.length === 0 && q.isFetched && (
          <div className="p-6 flex flex-col items-center gap-3 text-xs text-fg-dim">
            <span>No chats.</span>
            <button
              type="button"
              onClick={() => q.refetch()}
              disabled={q.isFetching}
              className="px-3 py-1.5 rounded-md border border-border text-fg hover:border-border-light hover:bg-bg-elev-1 disabled:opacity-50 inline-flex items-center gap-1.5"
            >
              <span className={cn("inline-block", q.isFetching && "animate-spin")}>↻</span>
              <span>{q.isFetching ? "Refreshing…" : "Refresh"}</span>
            </button>
          </div>
        )}
        {/* Skeleton rows during the cold first fetch — only when we have
         *  NO seed rows to render in their place. With the local-DB seed
         *  populated, `displayRows` already paints clickable placeholders
         *  and skeletons would just flash in front of them. */}
        {!q.isError && rows.length === 0 && !q.isFetched && !isFromSeed && (
          Array.from({ length: 10 }).map((_, i) => (
            <div
              key={`skel-${i}`}
              className="px-3 py-2.5 border-b border-border/40 flex items-center gap-3 animate-pulse"
            >
              <div className="w-9 h-9 rounded-full bg-bg-elev-1 shrink-0" />
              <div className="flex-1 min-w-0 space-y-1.5">
                <div className="h-2.5 w-2/5 bg-bg-elev-1 rounded" />
                <div className="h-2 w-4/5 bg-bg-elev-1/60 rounded" />
              </div>
            </div>
          ))
        )}
        {sortedDisplayRows.map((c) => {
          const accountId = c.__accountId ?? (scope.kind === "model" ? scope.accountId : "");
          const acc = accountById.get(accountId);
          const isSelected =
            selected?.accountId === accountId && selected?.fanId === c.withUser.id;
          // Priority: team-set nickname (from our SQLite, stitched onto
          // /users/list by the relay) → OF display name → OF username
          // → numeric id. Reads from the per-fan profile cache first (the
          // stable side channel that enrichWithUsers populates), falling
          // back to whatever's on the chat row — so a chats refetch that
          // drops back to slim withUser doesn't make the rail revert.
          const profile = profileMap.get(`${accountId}:${c.withUser.id}`);
          const fanName = decodeHtmlEntities(
            profile?.displayName ||
            c.withUser.displayName ||
            profile?.customNickname ||
            c.withUser.customNickname ||
            profile?.name ||
            c.withUser.name ||
            profile?.username ||
            c.withUser.username ||
            `fan ${c.withUser.id}`,
          );
          const avatarUrl = proxyImage(c.withUser.avatar || profile?.avatar || null, accountId);
          const spend = spendMap.get(`${accountId}:${c.withUser.id}`);
          const lastPurchase = lastPurchaseMap.get(`${accountId}:${c.withUser.id}`) ?? null;
          const recentSpend = recentSpendMap.get(`${accountId}:${c.withUser.id}`) ?? 0;
          // Prefer the batch /users/list?view=x lifetime total; if that
          // briefly reports 0 (OF cache lag), fall back to the windowed
          // sum from the transactions feed so the chip stops showing a
          // phantom $0 next to a populated last-buy timestamp.
          const displaySpendCents = Math.max(spend?.spend_cents ?? 0, recentSpend);
          return (
            <button
              key={`${accountId}:${c.withUser.id}`}
              type="button"
              data-chat-row
              onClick={() => {
                // No optimistic mark-as-read patch here — ChatSurface owns
                // the timing (1.2s grace window). Firing it on click was
                // overriding that delay so the blue dot vanished instantly.
                onSelect({ accountId, fanId: c.withUser.id, chat: c });
              }}
              onMouseEnter={() => scheduleRowPrefetch(accountId, c.withUser.id)}
              onMouseLeave={cancelRowPrefetch}
              onFocus={() => scheduleRowPrefetch(accountId, c.withUser.id)}
              onBlur={cancelRowPrefetch}
              // Touch never fires mouseenter/focus before the tap, so the
              // hover-prefetch strategy is dead on phone. Fire the prefetchers
              // directly (skipping the 120ms dwell — the tap IS the intent).
              // prefetchQuery no-ops while the query is fresh, so a mouse user
              // who already warmed this row on hover issues nothing extra.
              onPointerDown={() => {
                prefetchMessages(accountId, c.withUser.id);
                prefetchVault(accountId);
              }}
              className={cn(
                "w-full text-left px-3 py-2.5 border-b border-border/40",
                "flex items-start gap-2.5 hover:bg-bg-elev-1 transition-colors",
                isSelected && "bg-bg-elev-1",
              )}
            >
              {scope.kind === "all" && (
                <span
                  className="hidden lg:block mt-1 w-2 h-2 rounded-full shrink-0 border border-black/20"
                  style={{ background: acc?.color || "#666" }}
                  title={acc?.nickname || accountId}
                />
              )}
              <div className={cn(
                "w-9 h-9 rounded-full bg-bg-elev-1 grid place-items-center text-xs overflow-hidden shrink-0",
                // Muted chats read as grayed-out at a glance. Dimming the
                // avatar + content (not the row button) keeps the unread
                // badge sibling below at full opacity.
                c.isMutedNotifications && "opacity-60",
              )}>
                {avatarUrl ? (
                  <img
                    src={avatarUrl}
                    alt=""
                    loading="lazy"
                    decoding="async"
                    // Browser hint: avatars are decorative, never above
                    // the fold of attention. Yields network/decode budget
                    // to message media + the active chat surface.
                    fetchPriority="low"
                    className="w-full h-full object-cover"
                  />
                ) : (
                  <span className="text-fg-dim">{fanName.slice(0, 1).toUpperCase()}</span>
                )}
              </div>
              <div className={cn("min-w-0 flex-1", c.isMutedNotifications && "opacity-60")}>
                <div className="flex items-center justify-between gap-2">
                  <span className="flex items-center gap-1 min-w-0">
                    {c.isMutedNotifications && (
                      <span className="text-fg-dim shrink-0 text-[11px]" title="Muted notifications">🔕</span>
                    )}
                    <span className="text-sm font-medium truncate">{fanName}</span>
                  </span>
                  {/* Below lg the 8px colour dot is hidden and AccountRoster
                   *  (hidden lg:block) is gone, so this named chip is the only
                   *  model identifier on the row. lg:hidden ⇒ nothing renders
                   *  at ≥1024px, where the dot + roster are back. */}
                  {scope.kind === "all" && (
                    <span
                      className="lg:hidden text-[10px] leading-none px-1.5 py-0.5 rounded-full shrink-0 text-white max-w-[40%] truncate"
                      style={{ background: acc?.color || "#666" }}
                    >
                      {acc?.nickname || accountId}
                    </span>
                  )}
                  <span className="text-[10px] text-fg-dim shrink-0">
                    {fmtTime(c.lastMessage?.createdAt)}
                  </span>
                </div>
                <div className="text-xs text-fg-dim truncate">
                  {previewText(c)}
                </div>
                {(displaySpendCents > 0 || lastPurchase || spend?.subscription_status) && (() => {
                  const status = spend
                    ? interpretSubStatus(spend.subscription_status, spend.expired_at)
                    : null;
                  const toneClass =
                    status?.tone === "err" ? "text-err"
                    : status?.tone === "warn" ? "text-warn"
                    : "text-fg-dim";
                  return (
                    <div className="text-[10px] text-fg-dim mt-0.5 flex items-center gap-1.5 flex-wrap">
                      {displaySpendCents > 0 && (
                        <span className="text-ok font-medium">
                          ${(displaySpendCents / 100).toFixed(0)}
                        </span>
                      )}
                      {lastPurchase && (
                        <span title={lastPurchase}>
                          · last buy {fmtRelTime(lastPurchase)}
                        </span>
                      )}
                      {status && status.short !== "—" && (
                        <span className={toneClass} title={spend?.expired_at ?? undefined}>
                          · {status.short === "Active" && spend?.expired_at
                              ? `renews ${fmtRelTime(spend.expired_at)}`
                              : status.short === "Won't renew" && spend?.expired_at
                                ? `ends ${fmtRelTime(spend.expired_at)}`
                                : status.short}
                        </span>
                      )}
                    </div>
                  );
                })()}
              </div>
              {/* Status pill priority:
                 *   • Blue badge (count) → genuinely unread, OF said so
                 *   • Yellow dot         → read but fan sent the last msg
                 *                          ("I owe a reply")
                 *   • Nothing            → we sent last; ball's in their court
                 *
                 *  `accountId` is the model's OF user id (string);
                 *  `lm.fromUser.id` is a number, so the numeric compare
                 *  is the right one. */}
              {(() => {
                const lm = c.lastMessage;
                const myId = Number(accountId);
                const lastFromFan =
                  !!lm && lm.fromUser?.id != null && lm.fromUser.id !== myId;
                const unreadCount = c.unreadMessagesCount ?? 0;
                if (c.hasUnread || unreadCount > 0) {
                  return (
                    <span
                      className="mt-2.5 min-w-[18px] h-[18px] px-1 rounded-full bg-info text-white text-[10px] font-semibold grid place-items-center shrink-0"
                      title={`Unread${unreadCount ? ` (${unreadCount})` : ""}`}
                    >
                      {unreadCount > 0 ? unreadCount : ""}
                    </span>
                  );
                }
                if (lastFromFan) {
                  return <span className="mt-3 w-2 h-2 rounded-full bg-warn shrink-0" title="Read · awaiting your reply" />;
                }
                return null;
              })()}
            </button>
          );
        })}
        <div
          ref={sentinelRef}
          className="h-10 flex items-center justify-center text-[11px] text-fg-dim px-2 text-center"
        >
          {oweReplyActive
            ? (q.isFetchingMore
                ? `Scanning… ${oweReplyMatches} found in ${oweReplyScanned}`
                : oweReplyMatches === 0
                  ? `No replies needed in last ${oweReplyScanned} chats.`
                  : oweReplyScanned >= OWE_REPLY_SCAN_CAP && q.hasMore
                    ? (
                      <button
                        type="button"
                        onClick={() => q.loadMore()}
                        className="text-accent underline underline-offset-2 md:no-underline md:underline-offset-auto md:hover:underline inline-flex items-center px-4 py-2 md:px-0 md:py-0 min-h-[40px] md:min-h-0"
                      >
                        scanned {oweReplyScanned} · {oweReplyMatches} owed · keep scanning
                      </button>
                    )
                    : !q.hasMore
                      ? `${oweReplyMatches} owed · scanned all ${oweReplyScanned}`
                      : "")
            : q.isFetchingMore
              ? "Loading more…"
              : q.isFetching && rows.length > 0
                ? "Refreshing…"
                : capped && q.hasMore
                ? (
                  <button
                    type="button"
                    onClick={() => setCapRows(allRows.length + SCROLL_CAP_STEP)}
                    className="text-accent underline underline-offset-2 md:no-underline md:underline-offset-auto md:hover:underline inline-flex items-center px-4 py-2 md:px-0 md:py-0 min-h-[40px] md:min-h-0"
                  >
                    loaded {allRows.length} chats · load more
                  </button>
                )
                : q.hasMore
                  // More chats exist but the panel isn't tall enough to
                  // scroll (small/minimized window, few rows, or the
                  // autofill burst cap was hit), so the IO sentinel never
                  // re-fires. Offer an explicit pull so the tail is always
                  // reachable without a scrollbar.
                  ? (
                    <button
                      type="button"
                      onClick={() => q.loadMore()}
                      className="text-accent underline underline-offset-2 md:no-underline md:underline-offset-auto md:hover:underline inline-flex items-center px-4 py-2 md:px-0 md:py-0 min-h-[40px] md:min-h-0"
                    >
                      loaded {allRows.length} chats · load more
                    </button>
                  )
                  : rows.length > 0
                    ? "End of inbox."
                    : ""}
        </div>
      </div>
    </div>
  );
}

function FilterChip({
  active, onClick, children, title, count,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
  title?: string;
  count?: number | null;
}) {
  const showCount = count != null && count > 0;
  return (
    <button
      type="button"
      onClick={onClick}
      title={title}
      className={cn(
        "px-3 py-1.5 min-h-[36px] md:px-2 md:py-0.5 md:min-h-0 rounded-full border transition-colors whitespace-nowrap shrink-0 inline-flex items-center gap-1",
        active
          ? "bg-accent/15 text-accent border-accent/30"
          : "bg-transparent text-fg-dim border-border hover:border-border-light",
      )}
    >
      <span>{children}</span>
      {showCount && (
        <span
          className={cn(
            "px-1 min-w-[16px] text-center rounded-full text-[10px] leading-[14px]",
            active ? "bg-accent/30 text-accent" : "bg-bg-elev-1 text-fg-dim",
          )}
        >
          {count}
        </span>
      )}
    </button>
  );
}

/** Modal-ish popover listing every pinnable list (pinned + unpinned)
 *  with a checkbox toggle. Mirrors /ui/'s folder picker. Closes on outside
 *  click or Esc. */
function FolderPicker({
  accountId, onClose,
}: { accountId: string; onClose: () => void }) {
  const all = useAllChatFolders(accountId);
  const pin = usePinChatFolder(accountId);
  const rows = all.data ?? [];

  // Close on Esc
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div className="fixed inset-0 z-50 bg-black/40" onClick={onClose}>
      <div
        onClick={(e) => e.stopPropagation()}
        className="absolute top-24 left-1/2 -translate-x-1/2 w-[min(320px,calc(100vw-1.5rem))] max-h-[70vh] flex flex-col bg-panel border border-border rounded-lg shadow-xl"
      >
        <div className="px-3 py-2 border-b border-border text-sm font-semibold">
          Pin folders to chat sidebar
        </div>
        <div className="flex-1 overflow-y-auto">
          {all.isFetching && rows.length === 0 && (
            <div className="p-4 text-xs text-fg-dim">Loading…</div>
          )}
          {all.isError && (
            <div className="p-4 text-xs text-err">
              {describeLoadError(all.error as Error)}
            </div>
          )}
          {!all.isFetching && rows.length === 0 && !all.isError && (
            <div className="p-4 text-xs text-fg-dim">
              No pinnable lists yet — create one in OF first.
            </div>
          )}
          {rows.map((f) => {
            const id = String(f.id);
            const checked = !!f.isPinnedToChat;
            return (
              <label
                key={id}
                className="flex items-center gap-2 px-3 py-3 md:py-2 text-sm md:text-xs hover:bg-bg-elev-1 cursor-pointer"
              >
                <input
                  type="checkbox"
                  checked={checked}
                  disabled={pin.isPending}
                  onChange={(e) =>
                    pin.mutate({ listId: f.id, pinned: e.target.checked })
                  }
                />
                <span className="flex-1 truncate">
                  {f.name || `List #${id}`}
                </span>
                <span className="text-fg-dim shrink-0">
                  {f.usersCount ?? f.subscribersCount ?? 0}
                </span>
              </label>
            );
          })}
        </div>
        <div className="px-3 py-2 border-t border-border flex justify-end">
          <button
            type="button"
            onClick={onClose}
            className="text-xs px-3 py-1 rounded border border-border hover:border-border-light"
          >
            Done
          </button>
        </div>
      </div>
    </div>
  );
}

/** Tag the preview with "you:" when the last message came from us — this
 *  is huge for triage: at a glance you see whether the fan replied or is
 *  still waiting on us. The model account's id is the row's __accountId
 *  (which == that account's OF user id). */
function previewText(c: OFChatItem): string {
  const lm = c.lastMessage;
  if (!lm) return "";
  const fromMe =
    lm.fromUser?.id != null && c.__accountId != null &&
    String(lm.fromUser.id) === c.__accountId;
  const prefix = fromMe ? "you: " : "";
  // Quick visual cues so triage doesn't need opening every row:
  //   💰  tip       — money came in
  //   🔒  PPV       — paid content sent/received
  //   📎 N media    — free media only
  const marker =
    lm.isTip ? "💰 " :
    (lm.isFree === false || lm.lockedText) ? "🔒 " :
    "";
  if (lm.text) return prefix + marker + stripHtml(lm.text);
  if (lm.mediaCount) return `${prefix}${marker || "📎 "}${lm.mediaCount} media`;
  return (prefix + marker).trim();
}

function stripHtml(s: string): string {
  return s.replace(/<[^>]+>/g, "").trim();
}


function fmtTime(iso: string | undefined | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const now = new Date();
  const isToday = d.toDateString() === now.toDateString();
  if (isToday) {
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }
  const diffDays = (now.getTime() - d.getTime()) / 86_400_000;
  if (diffDays < 7) return d.toLocaleDateString([], { weekday: "short" });
  return d.toLocaleDateString([], { month: "short", day: "numeric" });
}
