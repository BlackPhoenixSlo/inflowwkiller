"use client";

/**
 * useChatList — fetches the inbox chat list, with built-in support for
 * "all models" (Unified Inbox) fan-out.
 *
 * Scope mapping:
 *   • scope.kind === "model" → single call to /api/of/v2/chats with
 *     X-Account-Id pinned to that model.
 *   • scope.kind === "all"   → parallel calls across every account that
 *     has a session. We merge by lastMessage.createdAt, tag each row with
 *     __accountId so the UI can show the colored dot.
 *
 * Polling: a 90s background poll (5min while the tab is hidden) that re-reads
 * the HEAD PAGE ONLY — see `useHeadPagePoll` below for why, and for what the
 * deeper pages rely on instead. SSE events drive cheaper between-poll patches
 * (Phase B.2); the Refresh button is the full re-walk.
 */

import { useCallback, useDeferredValue, useEffect, useRef } from "react";
import {
  hashKey,
  keepPreviousData,
  useInfiniteQuery,
  useQueryClient,
  type InfiniteData,
  type QueryClient,
  type QueryKey,
} from "@tanstack/react-query";

import { useScope } from "@/contexts/ScopeContext";
import { relay, type OFChatItem, type OFChatsResp, type OFUserMini } from "@/lib/relay";
import { perfDelivered, perfError, perfLog, perfOpId, perfPaintPending } from "@/lib/perfLog";
import { useActiveAccounts } from "./useAccounts";
import { useAllModelsInclude } from "./useAllModelsInclude";

const PAGE_SIZE = 25;
/** Background poll cadence: foreground, and while the document is hidden. */
const POLL_MS = 90_000;
const HIDDEN_POLL_MS = 5 * 60_000;

interface ChatListParams {
  filter?: "unread" | "pinned" | "priority" | null;
  /** Custom fan-list id to filter by (e.g. a pinned chat-folder). */
  listId?: string | null;
  query?: string | null;
  limit?: number;
}

interface UserListResp { [id: string]: OFUserMini & {
  avatarThumbs?: { c50?: string; c144?: string };
  isActive?: boolean;
  lastSeen?: string | null;
} }

function normalizeChats(resp: OFChatsResp, accountId: string): OFChatItem[] {
  const list = resp.list || resp.chats || [];
  // OF returns `unreadMessagesCount` (number); the rest of the app reads
  // `hasUnread` (boolean). Derive it here so every consumer — the dot,
  // filter chips, mark-all-read, SSE handlers — sees a consistent flag.
  return list.map((c) => {
    const cnt =
      (c as OFChatItem & { unreadMessagesCount?: number }).unreadMessagesCount ?? 0;
    return {
      ...c,
      __accountId: accountId,
      unreadMessagesCount: cnt,
      hasUnread: cnt > 0 || !!c.hasUnread,
    };
  });
}

/** OF's /chats only returns `{withUser: {id, _view}}`. We enrich names/
 *  avatars in two passes:
 *
 *  (1) **Local SQLite first.** One cheap `/admin/fans/{aid}/by-ids` call
 *      against our `fans` table — the WS transcoder writes names+avatars
 *      whenever a message lands, so any fan the model has ever talked to
 *      is already there. This pass is INSTANT and pays zero upstream cost.
 *
 *  (2) **Fill the gaps from OF.** Only ids we have no local row for hit
 *      `/users/list`, in chunks of 50 (OF's hard limit). The first chunk
 *      runs eagerly so visible-but-unknown fans get a name fast; the
 *      rest are awaited sequentially to avoid the 8-parallel storm that
 *      was throttling the vault picker. Every OF call carries
 *      `priority: "background"` so a user-initiated fetch (vault open,
 *      chat click) jumps the queue ahead via the relay's lane semaphore.
 *
 *  Profiles land in the per-fan `["of-user", aid, fid]` query cache. The
 *  ChatList row observes that cache, so even when chats refetches drop a
 *  slim version of the row, the rail label keeps the enriched name. */
async function enrichWithUsers(
  chats: OFChatItem[],
  accountId: string,
  qc: QueryClient,
): Promise<OFChatItem[]> {
  if (chats.length === 0) return chats;
  const ids = Array.from(new Set(chats.map((c) => c.withUser.id))).filter(Boolean);
  if (ids.length === 0) return chats;

  const byId = new Map<number, {
    id: number;
    name?: string;
    username?: string;
    avatar?: string | null;
    isActive?: boolean;
    lastSeen?: string | null;
    customNickname?: string | null;
    displayName?: string | null;
    notice?: string | null;
  }>();

  // ── Pass 1: local SQLite (zero upstream) ────────────────────────────
  try {
    const qs = new URLSearchParams();
    qs.set("ids", ids.join(","));
    const local = await relay.get<{
      fans: Record<string, {
        id: number;
        name: string | null;
        username: string | null;
        avatar: string | null;
        customNickname: string | null;
      }>;
    }>(`/admin/fans/${accountId}/by-ids?${qs.toString()}`);
    for (const [k, f] of Object.entries(local.fans || {})) {
      const nid = Number(k);
      if (!Number.isFinite(nid)) continue;
      // Treat a row as "useful" only if it has at least a display name.
      // A bare row with just an id wouldn't save the OF round-trip.
      if (!f.name && !f.username) continue;
      const profile = {
        id: nid,
        name: f.name ?? undefined,
        username: f.username ?? undefined,
        avatar: f.avatar ?? null,
        customNickname: f.customNickname ?? null,
      };
      byId.set(nid, profile);
      qc.setQueryData<Record<string, unknown> | undefined>(
        ["of-user", accountId, nid],
        (prev) => ({
          ...(prev ?? {}),
          id: nid,
          name: profile.name,
          username: profile.username,
          avatar: profile.avatar,
          customNickname:
            profile.customNickname
            ?? (prev as { customNickname?: string | null } | undefined)?.customNickname
            ?? null,
        }),
      );
    }
  } catch (err) {
    // Local lookup failing isn't fatal — we'll just hit OF for everything.
    console.warn("[chats] local fans/by-ids failed", err);
  }

  // ── Pass 2: fill gaps from OF (background priority, sequential) ─────
  const missing = ids.filter((id) => !byId.has(id));
  if (missing.length > 0) {
    const chunks: number[][] = [];
    for (let i = 0; i < missing.length; i += 50) chunks.push(missing.slice(i, i + 50));

    const fetchChunk = async (chunk: number[]) => {
      const qs = new URLSearchParams();
      for (const id of chunk) qs.append("ids", String(id));
      qs.set("view", "m");
      try {
        const resp = await relay.get<UserListResp>(
          `/api/of/v2/users/list?${qs.toString()}`,
          { accountId, priority: "background" },
        );
        for (const [k, u] of Object.entries(resp || {})) {
          const nid = Number(k);
          if (!Number.isFinite(nid)) continue;
          const profile = {
            id: nid,
            name: u.name,
            username: u.username,
            avatar: u.avatarThumbs?.c50 || u.avatarThumbs?.c144 || u.avatar || null,
            isActive: u.isActive,
            lastSeen: u.lastSeen ?? null,
            customNickname: u.customNickname ?? null,
            displayName: u.displayName ?? null,
            notice: u.notice ?? null,
          };
          byId.set(nid, profile);
          qc.setQueryData<Record<string, unknown> | undefined>(
            ["of-user", accountId, nid],
            (prev) => ({
              ...(prev ?? {}),
              id: nid,
              name: u.name,
              username: u.username,
              avatar: profile.avatar,
              customNickname:
                profile.customNickname
                ?? (prev as { customNickname?: string | null } | undefined)?.customNickname
                ?? null,
              displayName: profile.displayName,
              notice: profile.notice,
            }),
          );
        }
      } catch (err) {
        // One chunk failing shouldn't blank the rest of the inbox.
        console.warn("[chats] enrich users/list failed", err);
      }
    };

    // First chunk eagerly (so the visible top of the list lands fast);
    // the rest awaited sequentially so we never pile more than one
    // background /users/list onto the relay's per-account lane at a time.
    await fetchChunk(chunks[0]);
    for (let i = 1; i < chunks.length; i++) {
      // eslint-disable-next-line no-await-in-loop
      await fetchChunk(chunks[i]);
    }
  }

  return chats.map((c) => {
    const u = byId.get(c.withUser.id);
    if (!u) return c;
    // Note: `isActive` on /users/list is "account exists/not banned",
    // NOT live online presence. OF doesn't expose presence on this path,
    // so we don't pretend — `isOnline` stays falsy unless OF gives it.
    return { ...c, withUser: { ...c.withUser, ...u } };
  });
}

/** Sort newest-first by lastMessage.createdAt. Falls back to fan id so
 *  rows without a lastMessage still get a stable order. */
function compareChats(a: OFChatItem, b: OFChatItem): number {
  const ta = a.lastMessage?.createdAt ?? "";
  const tb = b.lastMessage?.createdAt ?? "";
  if (ta && tb && ta !== tb) return tb.localeCompare(ta);
  return (b.withUser?.id || 0) - (a.withUser?.id || 0);
}

interface ChatsPage {
  rows: OFChatItem[];
  hasMore: boolean;
}

/** Paint-attribution key for one list view. Exported so ChatList and
 *  fetchPage can't drift on the format. Deliberately coarser than the query
 *  key: `limit` and the unified-scope account set don't change which rail
 *  the user is looking at, and a re-key mid-fetch would strand the op. */
export function listPaintKey(
  scope: ReturnType<typeof useScope>["scope"],
  filter: ChatListParams["filter"],
  listId: ChatListParams["listId"],
  query: ChatListParams["query"],
): string {
  const scopeKey = scope.kind === "model" ? `model:${scope.accountId}` : "all";
  return `chat.list:${scopeKey}:${filter ?? ""}:${listId ?? ""}:${query ?? ""}`;
}

async function fetchPage(
  scope: ReturnType<typeof useScope>["scope"],
  accounts: ReturnType<typeof useActiveAccounts>,
  offset: number,
  filter: ChatListParams["filter"],
  listId: ChatListParams["listId"],
  query: ChatListParams["query"],
  limit: number,
  qc: QueryClient,
  // Named rather than positional. At eight parameters a trailing
  // `..., qc, false, "background")` told the reader nothing about which knob
  // was which — and the prefetch call site already reads
  // `scope, [], 0, null, null, null, limit, qc`.
  //
  // `priority` is explicit, never ambient: the relay's `_current_priority`
  // defaults to 'user' outside a request context, so an untagged call silently
  // eats one of the per-account lane's reserved user slots. Only a click waits
  // on 'user'; the 90s head poll and the hover prefetch are 'background'.
  { refresh = false, priority = "user" }: {
    refresh?: boolean;
    priority?: "user" | "background";
  } = {},
): Promise<ChatsPage> {
  const qs = new URLSearchParams();
  qs.set("limit", String(limit));
  qs.set("offset", String(offset));
  qs.set("order", "recent");
  if (filter) qs.set("filter", filter);
  if (listId) qs.set("list_id", listId);
  if (query) qs.set("query", query);
  const path = `/api/of/v2/chats?${qs.toString()}`;

  // Per-page perf op. offset=0 is "load messages" / first paint; offset>0
  // is "load next N messages" via infinite scroll. We log filter/listId/
  // query so a slow filter switch is identifiable.
  const opId = perfOpId("chat.list");
  perfLog(opId, "chat.list", "requested", {
    scope: scope.kind, offset, limit, filter, listId, query,
    phase: offset === 0 ? "initial" : "page",
    accountCount: scope.kind === "all" ? accounts.length : 1,
  });
  // Only a user-facing first page registers for a `painted` stamp — later
  // offsets append below the fold, and the background callers that reuse this
  // function (the 90s head poll, the roster hover-prefetch) must not: their
  // splice may commit nothing, and a dangling registration would attribute
  // whatever commit happens NEXT (an SSE bump, minutes later) to their op id.
  // ChatList emits the matching `painted` when rows reach the DOM.
  if (offset === 0 && priority === "user") {
    perfPaintPending(listPaintKey(scope, filter, listId, query), opId);
  }

  if (scope.kind === "model") {
    try {
      const resp = await relay.get<OFChatsResp>(
        path, { accountId: scope.accountId, priority }, undefined, { refresh },
      );
      const raw = normalizeChats(resp, scope.accountId);
      perfDelivered(opId, "chat.list", { count: raw.length, hasMore: !!resp.hasMore });
      // Kick off enrichment in the BACKGROUND so the rail paints immediately.
      // Enrichment writes per-fan profiles to the ["of-user", aid, fid] cache;
      // the row component observes that cache and picks up names + nicknames
      // as soon as /users/list lands. Don't await — that would slow cold paint.
      void enrichWithUsers(raw, scope.accountId, qc).catch(() => {});
      return { rows: raw.sort(compareChats), hasMore: !!resp.hasMore };
    } catch (err) {
      perfError(opId, "chat.list", { message: (err as Error)?.message });
      throw err;
    }
  }

  // Unified: each account independently paginates at this offset. We treat
  // hasMore as true if ANY account reported hasMore — that's the safe choice
  // since we want the user to be able to fetch more from the accounts that
  // still have data.
  const results = await Promise.allSettled(
    accounts.map(async (acc) => {
      const r = await relay.get<OFChatsResp>(
        path, { accountId: acc.id, priority }, undefined, { refresh },
      );
      const raw = normalizeChats(r, acc.id);
      void enrichWithUsers(raw, acc.id, qc).catch(() => {});
      return { raw, hasMore: !!r.hasMore };
    }),
  );
  const merged: OFChatItem[] = [];
  let anyMore = false;
  let failed = 0;
  for (const r of results) {
    if (r.status === "fulfilled") {
      merged.push(...r.value.raw);
      if (r.value.hasMore) anyMore = true;
    } else {
      failed += 1;
    }
  }
  perfDelivered(opId, "chat.list", {
    count: merged.length, hasMore: anyMore, accountCount: accounts.length, failed,
  });
  return { rows: merged.sort(compareChats), hasMore: anyMore };
}

/**
 * prefetchModelChatList — warm ONE model's inbox chat list into the query cache
 * BEFORE the user swaps to it (hover / click prewarm), so opening it paints from
 * cache instead of a spinner. Prefetches only page 0 of the default (no-filter)
 * view — the exact key `useChatList` mounts with on a plain model swap — and is a
 * no-op when that cache is still fresh (respects the same 30s staleTime). Any
 * other active filter/list re-keys the query, so the prefetch simply misses;
 * it's best-effort and never throws (prefetchInfiniteQuery swallows errors).
 */
export function prefetchModelChatList(
  qc: QueryClient,
  accountId: string,
  limit: number = PAGE_SIZE,
): Promise<void> {
  if (!accountId) return Promise.resolve();
  const scope = { kind: "model", accountId } as const;
  return qc.prefetchInfiniteQuery({
    queryKey: ["chats", "model", accountId, null, null, null, limit] as const,
    initialPageParam: 0,
    queryFn: ({ pageParam }) =>
      fetchPage(
        scope, [], (pageParam as number) ?? 0, null, null, null, limit, qc,
        { priority: "background" },
      ),
    // MUST mirror useChatList's infinite-query config: this prefetch shares the
    // SAME query key as the live list, so an options mismatch (a missing
    // getNextPageParam) corrupts that query and the list fails to load with
    // "options.getNextPageParam is not a function". React Query v5 requires it.
    getNextPageParam: (lastPage: ChatsPage, allPages: ChatsPage[]) =>
      lastPage.hasMore ? allPages.length * limit : undefined,
    staleTime: 30_000,
  });
}

/**
 * useHeadPagePoll — the background refresh for the inbox list, scoped to PAGE 0.
 *
 * **Why not `refetchInterval`.** On an infinite query React Query re-walks
 * EVERY loaded page on an interval refetch, sequentially — and each page here
 * is itself the multi-account fan-out gated by the slowest account. A user
 * parked on page 5 paid five full fan-outs every 90s to re-read rows that had
 * not moved; roughly 27% of all inbox list traffic was that redundant re-read.
 * (Background waste and lane pressure — it was never the p99 tail.)
 *
 * **What replaces it.** Fetch offset 0 — the *same* per-account offset-0 fetch
 * page 0 was born from, NOT "the first N of the merged list", which is not a
 * stable cursor in unified scope where every account paginates independently —
 * and splice it over page 0, leaving pages 1..n exactly as they were.
 *
 * **Why deeper pages are left alone instead of de-duped against the fresh head.**
 *   • `getNextPageParam` derives the next offset from `allPages.length * limit`,
 *     so the page COUNT is the cursor. Stripping a row that got promoted into
 *     the head page would punch a hole in a deep page while the server's own
 *     offsets stay put, and the next `fetchNextPage` would skip a server row.
 *   • `useChatList`'s flatten step already de-dupes by (accountId, fanId) and
 *     walks page 0 first, so the FRESH copy renders and the stale deep copy is
 *     invisible. This is exactly how `useInboxRealtime`'s bump-to-top has always
 *     worked: it prepends to page 0 and leaves the old copy where it sits — and
 *     it still rewrites all pages from SSE, so deep rows keep moving without
 *     this poll.
 *
 * **Known, bounded gap.** A row displaced OFF the fresh head page (something
 * below it got a new message) now sits at an offset the stale page 1 predates,
 * so it is invisible until the next `loadMore`, SSE bump, or Refresh. That is
 * precisely why `refresh()` stays a FULL re-walk: it is the user's recovery path.
 *
 * **The poll is silent.** Splicing via `setQueryData` never flips the query's
 * `fetchStatus`, and ChatList drives the Refresh button's spinner + disabled
 * state and the row count off `isFetching` — so the background poll no longer
 * spins and disables that button every 90s. The one tick that still goes
 * through a real refetch is a key with NO pages (cold or errored), where a
 * spinner and an error state are exactly what should be showing.
 */
function useHeadPagePoll(args: {
  enabled: boolean;
  queryKey: QueryKey;
  fetchHead: () => Promise<ChatsPage>;
}) {
  const qc = useQueryClient();
  const latest = useRef(args);
  latest.current = args;

  const { enabled, queryKey } = args;
  // React Query's own key hash — the same function the cache uses to decide
  // which entry a key addresses, so the timer re-arms on exactly the changes
  // that move it to a different cache entry.
  const keyHash = hashKey(queryKey);

  useEffect(() => {
    if (!enabled) return;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let cancelled = false;

    const tick = async () => {
      const { queryKey: key, fetchHead } = latest.current;
      const state = qc.getQueryState(key);
      // A fetch is already running on this key (manual Refresh, filter switch,
      // an in-flight loadMore) — never race a write against it.
      if (state && state.fetchStatus !== "idle") return;
      if (!(state?.data as InfiniteData<ChatsPage> | undefined)?.pages?.length) {
        // No pages at all: this key is cold or its initial fetch errored (with
        // `placeholderData: keepPreviousData` the list can still be rendering
        // the PREVIOUS key's rows meanwhile). There is nothing to splice into,
        // and a retry is exactly the job a refetch owns — including the error
        // state, which on a list with no rows is the right thing to show.
        void qc.refetchQueries({ queryKey: key, exact: true });
        return;
      }
      try {
        const head = await fetchHead();
        if (cancelled) return;
        qc.setQueryData<InfiniteData<ChatsPage>>(key, (prev) => {
          // `prev` is whatever the cache holds NOW, which is not necessarily
          // what we checked before the await: the entry can have been reset or
          // collected meanwhile, and one page is not a list to splice into.
          if (!prev?.pages?.length) return prev;
          return { ...prev, pages: [head, ...prev.pages.slice(1)] };
        });
      } catch {
        // A poll the user never asked for must not paint the list red: the
        // cached pages stand and the next tick (or Refresh) tries again. Not
        // swallowed silently — `fetchPage` perfError'd on its way out.
      }
    };

    // Re-read `document.hidden` at each scheduling point rather than arming one
    // fixed interval — same shape as the function-form `refetchInterval` this
    // replaces: 90s in the foreground, 5min once the tab is hidden.
    const schedule = () => {
      const hidden = typeof document !== "undefined" && document.hidden;
      timer = setTimeout(run, hidden ? HIDDEN_POLL_MS : POLL_MS);
    };
    // Schedule-AFTER-settle, never a bare interval: at most one tick is ever in
    // flight, so a slow fan-out can't stack ticks on top of itself.
    const run = () => {
      void tick().finally(() => { if (!cancelled) schedule(); });
    };

    schedule();
    return () => { cancelled = true; if (timer) clearTimeout(timer); };
    // `keyHash` stands in for `queryKey` (read through `latest`): re-arm the
    // timer when the cache it writes to changes, not on every render.
  }, [enabled, keyHash, qc]);
}

export function useChatList(params: ChatListParams = {}) {
  const { scope } = useScope();
  const allAccounts = useActiveAccounts();
  const { excluded } = useAllModelsInclude();
  const qc = useQueryClient();
  const { filter, listId, query, limit = PAGE_SIZE } = params;

  // In "all" scope, the user's exclude set drops accounts from the fan-out.
  // We filter BEFORE computing accountKey so the query key re-keys on
  // exclude-set change; TanStack returns prior data only if cached.
  //
  // Deferred on purpose: re-keying tears down the whole unified list and
  // fires a page-0 fetch per included account, all batched into the same
  // render as the ScopeSwitcher checkbox the user just clicked — which is
  // why toggling felt laggy. Deferring lets the urgent render paint the
  // checkbox against the old key, then applies the re-key in a background
  // render; rapid successive toggles coalesce (interrupted background
  // renders restart with the newest set) instead of fanning out per click.
  const deferredExcluded = useDeferredValue(excluded);
  const accounts =
    scope.kind === "all"
      ? allAccounts.filter((a) => !deferredExcluded.has(a.id))
      : allAccounts;

  // Stable query key: unified mode fans out across all session-bearing
  // accounts, so cache invalidation depends on the *set* of ids.
  const accountKey =
    scope.kind === "model"
      ? scope.accountId
      : accounts.map((a) => a.id).sort().join(",");

  const queryKey = [
    "chats", scope.kind, accountKey,
    filter ?? null, listId ?? null, query ?? null, limit,
  ] as const;

  // One-shot flag set by `refresh()` so the next queryFn invocation
  // forwards `?refresh=1` to the relay, bypassing the Stage C backend
  // cache (`list_chats` namespace). Cleared after consumption so an
  // unrelated background poll doesn't accidentally bypass the cache.
  const refreshOnceRef = useRef(false);

  const enabled =
    scope.kind === "model" ? !!scope.accountId : accounts.length > 0;

  const q = useInfiniteQuery<ChatsPage>({
    queryKey,
    enabled,
    initialPageParam: 0,
    getNextPageParam: (lastPage, allPages) =>
      lastPage.hasMore ? allPages.length * limit : undefined,
    queryFn: ({ pageParam }) => {
      // Bypass is scoped to PAGE 0 of the refetch triggered by refresh() (the
      // flag lives until the refetch settles; queryFn no longer clears it on
      // first consumption). Page 0 forced-fresh rewarms the shared server
      // window (badge + list) and re-syncs the visible top of the inbox;
      // deeper pages ride the relay cache — the server-side invalidations
      // (WS message, reply, mark-read) already dropped them if anything
      // actually changed, so a stale warm hit means "nothing happened", while
      // forcing every loaded page × every account through a live ~3-call OF
      // assemble turned one deep-scrolled unified Refresh into an OF storm.
      const offset = (pageParam as number) ?? 0;
      const bypass = refreshOnceRef.current && offset === 0;
      return fetchPage(
        scope, accounts, offset, filter, listId, query, limit, qc,
        { refresh: bypass },
      );
    },
    staleTime: 30_000,
    // NO refetchInterval — it would re-walk every loaded page. The background
    // refresh is `useHeadPagePoll` below (90s foreground / 5min hidden, page 0
    // only); read its comment before reinstating anything here.
    refetchOnWindowFocus: false,
    placeholderData: keepPreviousData,
  });

  // Deliberately NOT memoised: `useHeadPagePoll` re-reads its args every render,
  // so a fresh closure each render is how the timer sees the current fan-out
  // set without re-arming. `refresh=false` — the poll rides the relay's Stage C
  // cache; forcing every background tick through a live OF assemble is the
  // storm we're avoiding.
  const fetchHead = () =>
    fetchPage(
      scope, accounts, 0, filter, listId, query, limit, qc,
      { priority: "background" },
    );

  useHeadPagePoll({ enabled, queryKey, fetchHead });

  // Flatten + de-dupe by (accountId, fanId). The realtime SSE handler also
  // moves rows around, so duplication can creep in across pages.
  const rows: OFChatItem[] = [];
  const seen = new Set<string>();
  for (const p of q.data?.pages ?? []) {
    for (const c of p.rows) {
      const key = `${c.__accountId ?? ""}:${c.withUser.id}`;
      if (seen.has(key)) continue;
      seen.add(key);
      rows.push(c);
    }
  }

  // Imperative "force-fresh" hook for the Refresh-inbox button. Sets the
  // bypass flag for the WHOLE refetch (every loaded page forwards ?refresh=1
  // so Stage C's `list_chats` cache is bypassed), cleared when it settles.
  // An unrelated background poll landing inside that window also bypasses —
  // harmless: it just re-reads OF once more.
  const refresh = useCallback(() => {
    refreshOnceRef.current = true;
    return q.refetch().finally(() => {
      refreshOnceRef.current = false;
    });
  }, [q]);

  return {
    ...q,
    // Mirror the previous useQuery surface so the component doesn't have
    // to know we switched to infinite mode.
    data: rows,
    hasMore: !!q.hasNextPage,
    loadMore: () => q.fetchNextPage(),
    isFetchingMore: q.isFetchingNextPage,
    refresh,
  };
}
