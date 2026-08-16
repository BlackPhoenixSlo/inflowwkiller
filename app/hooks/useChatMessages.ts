"use client";

/**
 * useChatMessages — message history for one chat, with cursor pagination
 * and a smart-refresh action.
 *
 * `chatAccountId` is REQUIRED — chats live under one account, so even in
 * Unified Inbox mode the UI must pass the account that owns the chat
 * (we tag this on the chat-list row as `__accountId`). The relay scopes
 * by X-Account-Id and would otherwise fall back to the active account.
 *
 * Pagination: OF uses `before_id` (older). The cursor is the lowest id we
 * have actually FETCHED from OF this mount (`oldestCursorRef`), not the
 * lowest id in the cache: the persisted cache can hold an old block (e.g.
 * welcome-era rows from a previous session) separated from the fetched
 * pages by a gap. Cursoring off the cache minimum would jump PAST that gap
 * to the chat's first message, OF would answer "nothing older", and the
 * unfetched middle became permanently unreachable ("Start of conversation"
 * over a hole). Walking from the fetch-derived cursor pages through the
 * gap instead, healing poisoned caches as the user scrolls. The cache
 * minimum is only a fallback while no fetch has resolved yet.
 *
 * Smart refresh: §10 from MESSAGING-FULL-RECAP. Page-1 (offset 0) only
 * unless the user clicks the refresh button.
 */

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useRef, useState } from "react";

import { relay, type OFMessage, type OFMessagesResp } from "@/lib/relay";
import type { SendFailure } from "@/lib/sendFailure";
import { eventBus } from "@/lib/events";
import { toUtcIso } from "@/hooks/useInboxRealtime";
import { perfDelivered, perfError, perfLog, perfOpId } from "@/lib/perfLog";

// 30 matches OF's own chat-list page size — enough that most chats need
// at most one or two scroll-up fetches before reaching the start.
const PAGE_SIZE = 30;
// Local mass-broadcast placeholder band — mirrors service/attribution.py's
// _MASS_PLACEHOLDER_BASE. Rows in [MIN, END) are per-recipient optimistic
// stand-ins written at mass-send time; OF never returns an id this high, so
// they can only enter the cache via the DB seed / emit_live SSE.
export const MASS_PLACEHOLDER_MIN = 5_000_000_000_000_000;
// Exclusive band end: 6e15 is where ledger-synthesized TIP rows begin
// (service/transaction_ingest.py _TIP_MSG_ID_BASE). Those are PERMANENT
// synthetic history — the WS pump never saw them, the DB seed is their only
// render path — so the staleness rules below must never touch them.
export const MASS_PLACEHOLDER_END = 6_000_000_000_000_000;
// A placeholder only exists to bridge the send→delivery window (OF's queue
// can lag minutes on a big audience). Once a fresh page proves the thread's
// history extends PAST the placeholder's send time by this much and OF still
// didn't corroborate it, it's a stale leftover (unsent broadcast / dead
// reconcile) — drop it.
const MASS_PLACEHOLDER_GRACE_MS = 10 * 60_000;
// Absolute ceiling on the bridge, matching the relay seed's 1h serve window.
// Needed for the QUIET-fan shape: when the blasts are the newest thing in the
// thread (fan never replies — or OF never delivered, e.g. an excluded fan),
// no fresh row ever lands PAST them, so the grace rule above can't retire
// them — pre-fix persisted caches would keep such placeholders forever.
const MASS_PLACEHOLDER_MAX_AGE_MS = 60 * 60_000;
const STALE_MS = 15_000;
// When SSE is down, poll aggressively — that's the only path new
// messages can reach the UI. When SSE is up, the api2_chat_message
// event pushes new rows in real time, so polling is just a safety net
// for missed events and can run much slower.
const POLL_MS_OFFLINE = 30_000;
const POLL_MS_REALTIME = 2 * 60_000;

export interface UseChatMessagesOpts {
  /** The model account that owns this chat. Required. */
  accountId: string | null;
  /** The fan's user id (== OF chat id). */
  fanId: number | null;
  /** Disable polling — used while the chat is closed. */
  enabled?: boolean;
}

export function useChatMessages({ accountId, fanId, enabled = true }: UseChatMessagesOpts) {
  const qc = useQueryClient();

  // Mirror eventBus connection status so the poll interval can stretch
  // out when SSE is delivering pushes (the common case) and snap back
  // to the 30s aggressive cadence the moment the connection drops.
  const [sseConnected, setSseConnected] = useState(() => eventBus.isConnected());
  useEffect(() => eventBus.onStateChange(setSseConnected), []);

  // Tracks the oldest positive id we've FETCHED from OF this mount — the
  // load-older cursor. Monotonically lowered (head refetches must not bounce
  // it back up past pages the user already scrolled to), and never derived
  // from cached rows: a persisted old block below a gap would fake a deeper
  // cursor than we've really paged to. Optimistic ids (≤ 0) never enter.
  const oldestCursorRef = useRef<number | null>(null);
  const lowerCursor = useCallback((page: OFMessage[]) => {
    const oldest = page.find((m) => Number(m.id) > 0)?.id;
    const n = Number(oldest);
    if (!Number.isFinite(n) || n <= 0) return;
    oldestCursorRef.current =
      oldestCursorRef.current == null ? n : Math.min(oldestCursorRef.current, n);
  }, []);
  // Reflects OF's `hasMore` flag from the most recent page. Default to
  // true so the auto-loader fires at least once; the first fetch will
  // settle it to the real value.
  const [hasMore, setHasMore] = useState(true);
  const [isLoadingOlder, setIsLoadingOlder] = useState(false);
  // Re-entrancy guard for the scroll-to-top trigger.
  const inflightRef = useRef(false);

  const queryKey = ["messages", accountId, fanId] as const;

  const query = useQuery<OFMessage[]>({
    queryKey,
    enabled: enabled && !!accountId && fanId != null,
    queryFn: async ({ signal }) => {
      if (!accountId || fanId == null) return [];
      const opId = perfOpId("chat.messages");
      perfLog(opId, "chat.messages", "requested", {
        accountId, fanId, phase: "initial", limit: PAGE_SIZE,
      });
      try {
        // Forward the abort signal so switching chats fast cancels the
        // previous fetch in flight (frees the relay slot, drops the
        // payload before it returns).
        const resp = await relay.get<OFMessagesResp>(
          `/api/of/v2/chats/${fanId}/messages?limit=${PAGE_SIZE}&order=desc`,
          { accountId },
          signal,
        );
        // OF returns newest-first. Reverse so the UI can scroll oldest→newest
        // top-to-bottom (the way humans read a thread).
        const list = (resp.list || []).slice().reverse();
        lowerCursor(list);
        setHasMore(!!resp.hasMore);
        perfDelivered(opId, "chat.messages", {
          count: list.length, hasMore: !!resp.hasMore, phase: "initial",
        });
        // The background refetchInterval re-runs this queryFn and React Query
        // overwrites the whole cache with the return value. A bare `return list`
        // would replace the merged cache with just the newest page — wiping any
        // load-older pages the user scrolled up to fetch (and any in-flight
        // optimistic rows). Merge the fresh top page into what's already cached
        // instead: fresh wins for overlapping ids, older loaded pages stay in
        // front, optimistic/just-arrived rows stay at the tail. On the first
        // fetch the cache is empty, so this returns `list` unchanged.
        const prev = qc.getQueryData<OFMessage[]>(queryKey) || [];
        return mergeTopPage(prev, list);
      } catch (err) {
        perfError(opId, "chat.messages", {
          message: (err as Error)?.message, aborted: signal?.aborted, phase: "initial",
        });
        throw err;
      }
    },
    // Final safety net for React key uniqueness + placeholder hygiene. The
    // cache is written by several paths (initial fetch, load-older merge,
    // optimistic send, reconcile, SSE append) AND OF itself sometimes returns
    // the same message twice in one page (pinned rows, pagination-boundary
    // overlap). Any of those can leave two rows with the same id, which makes
    // MessageList emit duplicate `key={String(m.id)}` children. Dedup here,
    // first-occurrence-wins, so the rendered list is always unique without
    // every writer having to defend independently.
    //
    // The expired-placeholder drop must ALSO live here (not only in
    // mergeTopPage) because mergeTopPage runs solely on a SUCCESSFUL fetch:
    // a rehydrated pre-fix localStorage snapshot would otherwise paint
    // long-dead mass blasts before the first fetch resolves, and keep them up
    // for the whole session if that fetch fails (expired OF session, 429).
    // structuralSharing keeps the reference stable when nothing changed.
    select: selectMessages,
    staleTime: STALE_MS,
    refetchInterval: enabled ? (sseConnected ? POLL_MS_REALTIME : POLL_MS_OFFLINE) : false,
    // Override the global `false`: with two windows showing the same
    // chat (inbox + popout), background-tab throttling slows the
    // popout's 30s poll to ~60s+. Refetching on focus means switching
    // to the popout catches it up instantly instead of staring at stale
    // data for up to a minute.
    refetchOnWindowFocus: true,
  });

  /** Load one older page using cursor pagination. Idempotent — overlapping
   *  calls are dropped via inflightRef so the scroll observer can fire
   *  freely without producing duplicate requests. */
  const loadOlder = useCallback(async (): Promise<{ added: number; hasMore: boolean }> => {
    if (!accountId || fanId == null) return { added: 0, hasMore: false };
    if (inflightRef.current) return { added: 0, hasMore };
    // Fetch-derived cursor first (see the header comment: the cache minimum
    // can sit below an unfetched gap). Cache is only the bootstrap fallback
    // for a restored cache whose first head fetch hasn't resolved yet.
    const current = qc.getQueryData<OFMessage[]>(queryKey) || [];
    const oldest = oldestCursorRef.current ?? current.find((m) => Number(m.id) > 0)?.id;
    if (oldest == null) return { added: 0, hasMore: false };

    inflightRef.current = true;
    setIsLoadingOlder(true);
    const opId = perfOpId("chat.messages");
    perfLog(opId, "chat.messages", "requested", {
      accountId, fanId, phase: "older", before_id: oldest, limit: PAGE_SIZE,
    });
    try {
      const resp = await relay.get<OFMessagesResp>(
        `/api/of/v2/chats/${fanId}/messages?limit=${PAGE_SIZE}&order=desc&before_id=${oldest}`,
        { accountId },
      );
      const older = (resp.list || []).slice().reverse();
      lowerCursor(older);
      setHasMore(!!resp.hasMore);
      perfDelivered(opId, "chat.messages", {
        count: older.length, hasMore: !!resp.hasMore, phase: "older",
      });
      if (older.length === 0) return { added: 0, hasMore: !!resp.hasMore };

      qc.setQueryData<OFMessage[]>(queryKey, (prev = []) =>
        mergeOlderPage(prev, older),
      );
      return { added: older.length, hasMore: !!resp.hasMore };
    } catch (err) {
      perfError(opId, "chat.messages", { message: (err as Error)?.message, phase: "older" });
      throw err;
    } finally {
      inflightRef.current = false;
      setIsLoadingOlder(false);
    }
  }, [accountId, fanId, qc, queryKey, hasMore, lowerCursor]);

  /** Force-refresh the top page. Mutates in place — Query handles the
   *  setQueryData internally via refetch. */
  const refresh = useCallback(async () => {
    await qc.invalidateQueries({ queryKey });
  }, [qc, queryKey]);

  /** Append a message locally (used by optimistic send + SSE handlers). */
  const appendLocal = useCallback(
    (msg: OFMessage) => {
      qc.setQueryData<OFMessage[]>(queryKey, (prev = []) => {
        const incoming = String(msg.id);
        if (prev.some((m) => String(m.id) === incoming)) return prev;
        return [...prev, msg];
      });
    },
    [qc, queryKey],
  );

  /** Replace a message by tempId (reconcile optimistic → server). */
  const reconcileLocal = useCallback(
    (tempId: number, serverMsg: OFMessage) => {
      qc.setQueryData<OFMessage[]>(queryKey, (prev = []) => {
        const opt = prev.find((m) => m._tempId === tempId);
        const serverId = String(serverMsg.id);
        // SSE can echo the outbound message (real id) before this HTTP
        // reconcile resolves. If that row already exists, stamping the
        // same id onto the optimistic row would create two rows with the
        // same React key. Instead, fold the optimistic media fallback
        // onto the existing real row and drop the placeholder.
        const existingReal = prev.find(
          (m) => m._tempId !== tempId && String(m.id) === serverId,
        );
        if (existingReal) {
          return prev
            .filter((m) => m._tempId !== tempId)
            .map((m) =>
              m === existingReal && opt ? mergeMedia(opt, m) : m,
            );
        }
        return prev.map((m) => (m._tempId === tempId ? mergeMedia(m, serverMsg) : m));
      });
    },
    [qc, queryKey],
  );

  /** Mark an optimistic message as failed (Retry button shows).
   *
   *  Takes the whole {@link SendFailure} rather than its fields: it is exactly
   *  what `parseSendFailure` returns, so the send path reads "parse, then
   *  store" with no shape in between. `refusedMediaIds` is absent for every
   *  failure that isn't about media (network, the 409 re-sale block, a plain
   *  500), which is why the bubble tests it rather than the reason text. */
  const failLocal = useCallback(
    (tempId: number, failure?: SendFailure) => {
      qc.setQueryData<OFMessage[]>(queryKey, (prev = []) => {
        return prev.map((m) =>
          m._tempId === tempId
            ? {
                ...m,
                _failed: true,
                _pending: false,
                _failedReason: failure?.reason,
                _failedMediaIds: failure?.refusedMediaIds,
              }
            : m,
        );
      });
    },
    [qc, queryKey],
  );

  /** Remove an optimistic message (used before retry). */
  const dropLocal = useCallback(
    (tempId: number) => {
      qc.setQueryData<OFMessage[]>(queryKey, (prev = []) => {
        return prev.filter((m) => m._tempId !== tempId);
      });
    },
    [qc, queryKey],
  );

  return {
    ...query,
    loadOlder,
    hasOlder: hasMore,
    isLoadingOlder,
    refresh,
    appendLocal,
    reconcileLocal,
    failLocal,
    dropLocal,
  };
}

/** Render-path guard: drop rows that repeat an id (keeping the first
 *  occurrence so render identity — scroll position, mounted Bubble state —
 *  stays stable) AND drop mass placeholders past the 1h bridge ceiling.
 *  Returns the same array reference when nothing changed, so React Query's
 *  structural sharing doesn't see a spurious change. Optimistic rows (id ≤ 0)
 *  carry distinct negative ids per send, so they're never collapsed here;
 *  ledger-tip rows (≥ MASS_PLACEHOLDER_END) are permanent and never dropped.
 *  `now` is injectable for tests. */
export function selectMessages(list: OFMessage[], now: number = Date.now()): OFMessage[] {
  const seen = new Set<string>();
  let changed = false;
  const out: OFMessage[] = [];
  for (const m of list) {
    const k = String(m.id);
    if (seen.has(k)) { changed = true; continue; }
    seen.add(k);
    const id = Number(m.id);
    if (id >= MASS_PLACEHOLDER_MIN && id < MASS_PLACEHOLDER_END) {
      const at = Date.parse(toUtcIso(m.createdAt));
      if (Number.isFinite(at) && now - at > MASS_PLACEHOLDER_MAX_AGE_MS) {
        changed = true;
        continue;
      }
    }
    out.push(m);
  }
  return changed ? out : list;
}

/** Canonical thread order: everything a real timestamp can place (real OF
 *  rows AND ledger-synthesized tip rows, 6e15 band) sorted oldest→newest by
 *  created_at; rows that represent "just now" or an in-flight bridge —
 *  optimistic sends (id ≤ 0) and mass placeholders (5e15 band) — stay pinned
 *  at the tail in their existing relative order.
 *
 *  Why a time sort and not id order: OF ids are time-ordered for REAL rows,
 *  but tip rows carry a SYNTHETIC id (6e15 + tx.id) that encodes nothing about
 *  time — id-ordering pinned every tip to the bottom of the thread, so a $50
 *  tip from March rendered below today's chat and vanished on scroll-up. The
 *  SSE patcher already positions live rows by created_at (insertByCreatedAt);
 *  this makes the HTTP fetch/merge path agree instead of fighting it.
 *
 *  The sort is STABLE on the input's existing order (arrival index tie-break),
 *  so real rows fed in id-order keep that order on a created_at tie — the
 *  common case is a no-op reorder. Returns the SAME array reference when
 *  nothing moved, so React Query's structural sharing sees no spurious change. */
export function orderThread(rows: OFMessage[]): OFMessage[] {
  const timed: OFMessage[] = [];
  const tail: OFMessage[] = [];
  for (const m of rows) {
    const id = Number(m.id);
    // Time-positioned: real OF rows (0 < id < 5e15) and ledger tips (≥ 6e15).
    // Tail-pinned: optimistic (id ≤ 0) and mass placeholders (5e15..6e15).
    const timePositioned =
      (Number.isFinite(id) && id > 0 && id < MASS_PLACEHOLDER_MIN) ||
      id >= MASS_PLACEHOLDER_END;
    (timePositioned ? timed : tail).push(m);
  }
  // Sort by created_at; tie-break by numeric id so rows sharing a timestamp
  // fall in id order (for real rows that IS time order — this reproduces the
  // old id-ordering exactly whenever timestamps collide, e.g. same-second
  // bursts — and drops a same-second tip just after the messages it followed).
  timed.sort((a, b) => {
    const ta = Date.parse(toUtcIso(a.createdAt));
    const tb = Date.parse(toUtcIso(b.createdAt));
    const da = Number.isFinite(ta) ? ta : 0;
    const db = Number.isFinite(tb) ? tb : 0;
    return da !== db ? da - db : Number(a.id) - Number(b.id);
  });
  const out = [...timed, ...tail];
  // Same-reference fast path: bail if the sort left everything where it was.
  let moved = out.length !== rows.length;
  for (let i = 0; !moved && i < out.length; i++) {
    if (out[i] !== rows[i]) moved = true;
  }
  return moved ? out : rows;
}

/** Merge a freshly-fetched top page (newest `PAGE_SIZE`, oldest→newest) into
 *  the existing cache without discarding load-older pages or in-flight
 *  optimistic rows. `fresh` wins for any overlapping id. A cached row not in
 *  `fresh` is classified by where its id falls relative to the page's id span:
 *
 *    • id < minFreshId       → scrolled-up history below the page → KEEP (front)
 *    • minFreshId..maxFreshId → INSIDE the page's span but OF didn't return it.
 *                               OF serves a contiguous newest slice, so a gap
 *                               means the message was deleted / unsent upstream
 *                               → DROP (else it lingers as an orphan pinned to
 *                               the bottom forever — e.g. a PPV you unsent on OF
 *                               that the local cache never let go of).
 *    • id > maxFreshId or ≤0  → newer than the page (poll race window) or an
 *                               in-flight optimistic row → KEEP (tail)
 *
 *  Mass placeholders (id ≥ MASS_PLACEHOLDER_MIN) get their own rule: their
 *  synthetic id sits above every real OF id, so the id-span drop above could
 *  never retire one — a stale placeholder (unsent broadcast, dead reconcile)
 *  would ride the "newer than the page" branch forever. Judge them by TIME
 *  instead — drop one when EITHER:
 *    • the fresh page's newest real row is past the placeholder's send time
 *      (+ grace for queue-delivery lag) and OF didn't corroborate it, OR
 *    • it's simply older than the 1h bridge ceiling (the quiet-fan shape:
 *      nothing newer ever lands, so the first rule alone can't retire it).
 *  A just-sent placeholder is still the newest thing → kept.
 *
 *  Returns `fresh` unchanged when there's nothing extra to keep, so structural
 *  sharing still sees a stable reference for the common case. When `fresh` is
 *  empty (no positive ids) minFreshId stays Infinity, so every cached REAL row
 *  falls into the < minFreshId branch and nothing real is dropped — an
 *  empty/failed fetch never nukes the cache (only expired placeholders go). */
export function mergeTopPage(
  prev: OFMessage[],
  fresh: OFMessage[],
  now: number = Date.now(),
): OFMessage[] {
  if (prev.length === 0) return fresh;
  const freshIds = new Set(fresh.map((m) => String(m.id)));
  let minFreshId = Infinity;
  let maxFreshId = -Infinity;
  let maxFreshAt = 0;
  for (const m of fresh) {
    const id = Number(m.id);
    if (!Number.isFinite(id) || id <= 0 || id >= MASS_PLACEHOLDER_MIN) continue;
    if (id < minFreshId) minFreshId = id;
    if (id > maxFreshId) maxFreshId = id;
    const at = Date.parse(toUtcIso(m.createdAt));
    if (Number.isFinite(at) && at > maxFreshAt) maxFreshAt = at;
  }
  const older: OFMessage[] = [];
  const tail: OFMessage[] = [];
  for (const m of prev) {
    if (freshIds.has(String(m.id))) continue;
    const id = Number(m.id);
    if (id >= MASS_PLACEHOLDER_MIN && id < MASS_PLACEHOLDER_END) {
      const at = Date.parse(toUtcIso(m.createdAt));
      const stale = Number.isFinite(at) && (
        maxFreshAt > at + MASS_PLACEHOLDER_GRACE_MS  // OF spoke past it without it
        || now - at > MASS_PLACEHOLDER_MAX_AGE_MS    // outlived the bridge outright
      );
      if (stale) continue;
      tail.push(m);  // just-sent bridge — keep until OF corroborates/retires it
      continue;
    }
    if (id > 0 && id < minFreshId) { older.push(m); continue; }
    if (id > 0 && id <= maxFreshId) continue;  // deleted/unsent on OF → drop
    tail.push(m);  // newer-than-page race row, or optimistic (id ≤ 0)
  }
  // orderThread pulls any tip rows (6e15 band) out of the `tail` bucket into
  // their chronological slot; real rows and the just-now tail keep their order.
  if (older.length === 0 && tail.length === 0) return orderThread(fresh);
  return orderThread([...older, ...fresh, ...tail]);
}

/** Merge a load-older page into the cache, keeping oldest→newest order even
 *  when the page lands INSIDE a gap — i.e. there are cached rows both older
 *  (a persisted welcome-era block) and newer (the fetched head pages) than it.
 *  The old blind-prepend assumed every fetched-older row predated the whole
 *  cache, which scrambled order the moment the cursor walked a gap.
 *
 *  Ordering is delegated to orderThread: real rows + ledger tips (6e15 band)
 *  sort by created_at (with an id tie-break that reproduces id-ordering for
 *  same-timestamp real rows), while optimistic sends (id ≤ 0) and mass
 *  placeholders (5e15 band) keep their tail position. For the common no-gap
 *  case the sort is a no-op reorder-wise. Returns `prev` unchanged when the
 *  page brings nothing new. */
export function mergeOlderPage(prev: OFMessage[], older: OFMessage[]): OFMessage[] {
  const have = new Set(prev.map((m) => String(m.id)));
  const dedup = older.filter((m) => !have.has(String(m.id)));
  if (dedup.length === 0) return prev;
  return orderThread([...prev, ...dedup]);
}

/** Merge optimistic media's `files` into a server response — OF's CDN
 *  takes ~2-3s to populate `files.{thumb,source}.url`, so the server
 *  response often comes back with media but no playable URLs. We swap
 *  the optimistic file urls in until the next refresh catches them. */
function mergeMedia(opt: OFMessage, server: OFMessage): OFMessage {
  if (!opt?.media?.length || !server?.media?.length) return { ...server, _pending: false, _tempId: opt._tempId };
  const byId = new Map<number, OFMessage["media"][number]>();
  for (const om of opt.media) if (typeof om.id === "number") byId.set(om.id, om);
  const mergedMedia = server.media.map((sm, i) => {
    if (sm.files?.thumb?.url || sm.files?.source?.url) return sm;
    const fallback =
      (typeof sm.id === "number" ? byId.get(sm.id) : undefined) || opt.media[i];
    return fallback?.files ? { ...sm, files: fallback.files } : sm;
  });
  return { ...server, media: mergedMedia, _pending: false, _tempId: opt._tempId };
}
