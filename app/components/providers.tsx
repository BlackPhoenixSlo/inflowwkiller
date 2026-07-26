"use client";

/**
 * Top-level client providers. Mounted once in layout.tsx, wraps the
 * entire tree.
 *
 * React Query staleTime/gcTime are already last-access-based; do not add
 * explicit last-used bookkeeping at the query layer.
 *
 * Order matters:
 *
 *   QueryClientProvider — must be outermost so every fetch flows through
 *                         the same cache. Children include the popout
 *                         tab too, which inherits via the localStorage
 *                         persister wired below.
 *   IsRestoringProvider — flips queries into "restoring" mode so they
 *                         read from cache but don't fire network until
 *                         the localStorage hydrate finishes.
 *   EmployeeProvider    — drives the picker modal + X-Employee-Id header.
 *   ScopeProvider       — passive holder for active scope.
 *
 * QueryClient is created once per browser session via useState's lazy
 * init — Next 16's React 19 strict mode would otherwise re-create it
 * on every render and lose all in-flight cache.
 *
 * **Persistence**: we wire `persistQueryClient` once on mount, which
 * mirrors selected query keys to localStorage so popout windows + repeat
 * tab-opens hydrate from disk. We pair it with `IsRestoringProvider` so
 * useQuery hooks DON'T fire network requests during the first render —
 * otherwise they'd race the async restore and the popout tab would
 * re-fetch data that's already sitting in localStorage. We tracked down
 * this race after pop-out kept reloading vault + messages even though
 * the inbox had already loaded them.
 */

import {
  QueryClient,
  QueryClientProvider,
  IsRestoringProvider,
} from "@tanstack/react-query";
import { ReactQueryDevtools } from "@tanstack/react-query-devtools";
import { persistQueryClient, removeOldestQuery } from "@tanstack/react-query-persist-client";
import { createSyncStoragePersister } from "@tanstack/query-sync-storage-persister";
import { useEffect, useState } from "react";

import { UserProvider } from "@/contexts/UserContext";
import { ChatterProvider } from "@/contexts/ChatterContext";
import { EmployeeProvider } from "@/contexts/EmployeeContext";
import { ScopeProvider } from "@/contexts/ScopeContext";
import { ErrorBoundary } from "@/components/ErrorBoundary";
import { installGlobalErrorHandlers } from "@/lib/errorReporter";
import { perfBoot } from "@/lib/perfLog";
import { registerImageSW } from "@/lib/registerImageSW";

// Bump when the persisted cache shape changes (added/removed fields on
// OFChatItem, etc.) so old localStorage entries get nuked on read instead
// of silently rendering as garbage.
// v3.2: fan-spend source switched from OF /users/list to local DB
// /admin/fans/{aid}/spend-batch (key renamed from "fan-spend-of" → "fan-spend").
// v3.3: force-evict stale persisted fan identity (of-user/chats/fan) after a
// batch of out-of-band nickname/note rewrites (gen_info backfill) left drawers
// showing pre-regen "<name>/Whale" + old notes across reloads.
// v3.4: evict persisted caches that may hold a poisoned, principal-agnostic
// ["accounts"] full-registry list (see useAccounts.ts — fixed to key by user).
const CACHE_BUSTER = "v3.4";
// Snapshot-level cap — if the user is inactive for this long, the whole
// localStorage cache gets dropped. Bumped to 30d so a 2-week vacation
// doesn't nuke everything; the per-query sliding window below handles
// the actual freshness policy.
const PERSIST_MAX_AGE = 30 * 24 * 60 * 60 * 1000;
// Per-query sliding window — a query persists only if it was used (read
// or refetched) within this many ms. Rolls forward on every mount /
// data update so heavily-used queries stay warm forever and forgotten
// ones age out.
const PER_QUERY_MAX_IDLE_MS = 7 * 24 * 60 * 60 * 1000;

// Keys we want shared across tabs. Anything not on this list stays
// in-memory only.
const PERSIST_PREFIXES = new Set([
  "chats",          // inbox sidebar — biggest popout win
  "chat-folders",   // pinned-folder chip strip — avoids 1s lag on tab switch
  "messages",       // per-chat thread; popout reuses the inbox's loaded pages
  "of-me",          // model account profile — small, reused everywhere
  "of-user",        // single-fan profile lookup (popout header)
  "vault-lists",    // folders for the Add-to-lists picker (kills 2s lag)
  "vault-history",  // per-fan sent/purchased history for picker badges
  "wall-media",     // ids posted on the wall — blue ring in vault picker
  "templates",      // composer's quick-reply picker
  "saved-replies",  // composer's saved replies
  "msg-image-desc", // 👁 what the AI saw in each photo HE sent. Tiny (mean 2.7
                    //   entries/fan) and effectively IMMUTABLE — a description
                    //   is recomputed only by an explicit re-read click — so
                    //   unlike the attribution overlay next door there's nothing
                    //   for a stale copy to get wrong. Persisting it means a
                    //   reload or "↗ pop out" paints captions with no round-trip.
  "fan",            // local fan-row drawer data
  "fan-spend",      // bulk lifetime-spend per chat row (24h staleTime)
  "fan-chat-media", // FanDrawer Sales + Unsold PPV galleries — slow OF
                    //   call (3-9s), 30min SWR, 24h gcTime. Surviving
                    //   tab reload / popout open avoids 6s of blank
                    //   "Sales" section every time the drawer re-mounts.
  "last-purchases", // payouts transactions feed — staleTime Infinity
  "employees",      // employee picker
  "accounts",       // active-models sidebar
  "chatter",        // chatter principal — me / owners / accounts
]);

function shouldPersistKey(queryKey: readonly unknown[]): boolean {
  const head = queryKey[0];
  return typeof head === "string" && PERSIST_PREFIXES.has(head);
}

export default function Providers({ children }: { children: React.ReactNode }) {
  const [queryClient] = useState(() => new QueryClient({
    defaultOptions: {
      queries: {
        // Treat everything as fresh for 3 days by default — chatters
        // re-open the tab across days and we don't want to re-fetch
        // half the inbox each time. Per-query overrides at the useQuery
        // call site for things that genuinely need freshness (chat list
        // 30s, online presence 30s, etc.)
        staleTime: 3 * 24 * 60 * 60 * 1000,
        // 7d to match PERSIST_MAX_AGE — keeps queries alive in memory
        // for as long as their persisted copy is valid.
        gcTime:    7 * 24 * 60 * 60 * 1000,
        // Don't refetch on window focus — feels noisy with SSE already
        // keeping things live. Re-enable per-query if a screen needs it.
        refetchOnWindowFocus: false,
        retry: 1,
      },
      mutations: {
        retry: 0,
      },
    },
  }));

  // Starts true so first-render useQuery hooks don't fire network requests
  // before localStorage hydration completes — that race is what made the
  // popout tab re-load vault + messages despite them being in localStorage.
  const [isRestoring, setIsRestoring] = useState(true);

  // Wire the global error reporter once per page load. This catches
  // window-level uncaught exceptions and unhandled promise rejections
  // and POSTs them to /admin/errors. Idempotent — safe under StrictMode
  // double-invoke.
  useEffect(() => {
    installGlobalErrorHandlers();
    // SW for image caching — no-op in dev, lazy register in prod. See
    // IMAGE_LOAD_PLAN.txt phase C.
    registerImageSW();
    // Side-effect import: perfLog's module-evaluation mints the tabId
    // and emits the `tab open` event. The call itself is a no-op; we
    // just want the import to land in the boot bundle so the open
    // event fires before lazy-loaded hooks first drag perfLog in.
    perfBoot();
  }, []);

  useEffect(() => {
    if (typeof window === "undefined") {
      setIsRestoring(false);
      return;
    }
    // Track per-query last-used time so the persistence layer can roll
    // a 7-day window forward on every mount / data update. dataUpdatedAt
    // doesn't bump on plain reads from cache (only on fetches), so for
    // long-staleTime queries we'd otherwise evict actively-used data.
    const lastUsed = new Map<string, number>();
    const queryCache = queryClient.getQueryCache();
    // Seed with anything already in cache (rehydrated from a previous
    // session before this effect ran).
    for (const q of queryCache.getAll()) {
      lastUsed.set(q.queryHash, q.state.dataUpdatedAt || Date.now());
    }
    const unsubLastUsed = queryCache.subscribe((event) => {
      // `observerAdded` fires when a component mounts and starts reading
      // the query — that's our "used" signal for infinite-staleTime
      // queries. `updated` fires on every state change (fetch lands,
      // setQueryData, etc.) and covers refetches.
      if (event.type === "observerAdded" || event.type === "updated") {
        lastUsed.set(event.query.queryHash, Date.now());
      } else if (event.type === "removed") {
        lastUsed.delete(event.query.queryHash);
      }
    });

    const persister = createSyncStoragePersister({
      storage: window.localStorage,
      key: "chatterly-rq-cache",
      // 250ms is short enough that hitting "↗ pop out" right after a
      // fetch lands still flushes the new data to localStorage before
      // the new tab reads it. 1000ms (the previous value) was long
      // enough to miss freshly-loaded queries.
      throttleTime: 250,
      // On QuotaExceededError, shed the oldest queries and retry instead of
      // giving up. Without this the cache pins the origin at the 5MB quota
      // and every OTHER localStorage write on the site (rail settings, dock
      // spots, notif history, exclude lists) silently fails forever.
      retry: removeOldestQuery,
    });
    const [unsubscribe, restorePromise] = persistQueryClient({
      queryClient,
      persister,
      maxAge: PERSIST_MAX_AGE,
      buster: CACHE_BUSTER,
      dehydrateOptions: {
        shouldDehydrateQuery: (q) => {
          // Only persist successful queries on the allowlist. Errored
          // ones aren't worth carrying over — they'll just re-trigger.
          if (q.state.status !== "success") return false;
          if (!shouldPersistKey(q.queryKey)) return false;
          // Per-query sliding window: drop from the snapshot if it
          // hasn't been touched in PER_QUERY_MAX_IDLE_MS.
          const used = lastUsed.get(q.queryHash) ?? q.state.dataUpdatedAt;
          return Date.now() - used < PER_QUERY_MAX_IDLE_MS;
        },
      },
    });
    restorePromise.finally(() => setIsRestoring(false));
    return () => {
      unsubLastUsed();
      unsubscribe();
    };
  }, [queryClient]);

  return (
    <QueryClientProvider client={queryClient}>
      <IsRestoringProvider value={isRestoring}>
        <UserProvider>
          <ChatterProvider>
            <EmployeeProvider>
              <ScopeProvider>
                <ErrorBoundary scope="root">{children}</ErrorBoundary>
              </ScopeProvider>
            </EmployeeProvider>
          </ChatterProvider>
        </UserProvider>
      </IsRestoringProvider>
      {/* Devtools only render in development; safe to leave mounted. */}
      {/* bottom-left so the floating button doesn't overlap the Composer's
       *  Send / Send-in-N controls in the chat surface. */}
      <ReactQueryDevtools initialIsOpen={false} buttonPosition="bottom-left" />
    </QueryClientProvider>
  );
}
