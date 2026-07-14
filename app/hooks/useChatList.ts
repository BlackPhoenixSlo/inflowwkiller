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
 * Polling: 60s, matching the desktop-app's cadence. SSE events drive
 * cheaper between-poll invalidations (Phase B.2).
 */

import { useCallback, useDeferredValue, useRef } from "react";
import { keepPreviousData, useInfiniteQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";

import { useScope } from "@/contexts/ScopeContext";
import { relay, type OFChatItem, type OFChatsResp, type OFUserMini } from "@/lib/relay";
import { perfDelivered, perfError, perfLog, perfOpId } from "@/lib/perfLog";
import { useActiveAccounts } from "./useAccounts";
import { useAllModelsInclude } from "./useAllModelsInclude";

const PAGE_SIZE = 25;

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

async function fetchPage(
  scope: ReturnType<typeof useScope>["scope"],
  accounts: ReturnType<typeof useActiveAccounts>,
  offset: number,
  filter: ChatListParams["filter"],
  listId: ChatListParams["listId"],
  query: ChatListParams["query"],
  limit: number,
  qc: QueryClient,
  refresh = false,
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

  if (scope.kind === "model") {
    try {
      const resp = await relay.get<OFChatsResp>(
        path, { accountId: scope.accountId }, undefined, { refresh },
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
        path, { accountId: acc.id }, undefined, { refresh },
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
      fetchPage(scope, [], (pageParam as number) ?? 0, null, null, null, limit, qc, false),
    // MUST mirror useChatList's infinite-query config: this prefetch shares the
    // SAME query key as the live list, so an options mismatch (a missing
    // getNextPageParam) corrupts that query and the list fails to load with
    // "options.getNextPageParam is not a function". React Query v5 requires it.
    getNextPageParam: (lastPage: ChatsPage, allPages: ChatsPage[]) =>
      lastPage.hasMore ? allPages.length * limit : undefined,
    staleTime: 30_000,
  });
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

  const q = useInfiniteQuery<ChatsPage>({
    queryKey,
    enabled:
      scope.kind === "model" ? !!scope.accountId : accounts.length > 0,
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
        scope, accounts, offset, filter, listId, query, limit, qc, bypass,
      );
    },
    staleTime: 30_000,
    // 90s in foreground; backs off to 5min when the document is hidden
    // (e.g. user switched away). Tanstack pauses intervals on hidden tabs
    // by default, but specifying explicitly also covers the "visible but
    // unfocused" case (other monitor, split screen) where the default
    // would still hammer at the foreground rate.
    refetchInterval: () =>
      typeof document !== "undefined" && document.hidden ? 5 * 60_000 : 90_000,
    refetchOnWindowFocus: false,
    placeholderData: keepPreviousData,
  });

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
