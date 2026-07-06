"use client";

/**
 * /inbox — Unified Inbox + per-model.
 *
 * Layout: two-pane. Left = ChatList (scope-aware). Right = ChatSurface
 * for the selected chat, or an empty placeholder.
 *
 * Why selection lives here: switching chats should unmount and remount
 * the ChatSurface (fresh hooks, fresh refs), which we do via `key=`.
 * That's the React-idiomatic answer to the desktop-app's `connectionId`
 * key pattern from §12.
 */

import { useEffect, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { ChatList, type ChatListSelection } from "@/components/chat/ChatList";
import { ChatSurface } from "@/components/chat/ChatSurface";
import { AccountRoster } from "@/components/inbox/AccountRoster";
import { useInboxRealtime } from "@/hooks/useInboxRealtime";
import { fetchAllVaultLists } from "@/hooks/useVaultMedia";
import { useActiveAccounts } from "@/hooks/useAccounts";
import { useScope } from "@/contexts/ScopeContext";
import { cn } from "@/lib/utils";
import { relay } from "@/lib/relay";

const ROSTER_W_COLLAPSED = "44px";
const ROSTER_W_EXPANDED = "260px";

export default function InboxPage() {
  const [selected, setSelected] = useState<ChatListSelection | null>(null);
  const [rosterExpanded, setRosterExpanded] = useState(false);
  useInboxRealtime();
  const qc = useQueryClient();
  const accounts = useActiveAccounts();
  const { scope } = useScope();

  // Clear the selected chat when the new scope no longer covers it.
  // Unified ("all") keeps everything; switching to a different model
  // hides the surface so the user picks a fan from the new model's list.
  useEffect(() => {
    if (!selected) return;
    if (scope.kind === "model" && selected.accountId !== scope.accountId) {
      setSelected(null);
    }
  }, [scope, selected]);

  // Background-warm the vault caches so the first VaultPicker open is
  // instant. Two waves, both tagged `X-Priority: background` so the
  // relay's per-account lane (4 total / 2 background slots, see
  // service/server.py `_priority_lane`) always keeps reserved slots for
  // a user click that lands mid-warm. Within an account, lists + first
  // page of media run in parallel. Across accounts, accounts also run
  // in parallel — lanes are per-account so they don't interfere, and
  // network is the only shared resource.
  //
  // Wave 1 (cheap, fires ~800ms after mount): vault-lists + vault-media
  //   first page. These are what VaultPicker reads on open. Together
  //   they cover the "click vault → grid is already there" case.
  //
  // Wave 2 (expensive, fires ~15s after mount): wall-media. This walks
  //   5 OF post pages per account and drives the blue "sent on wall"
  //   rings in the picker. The grid renders without it; rings just pop
  //   in when wave 2 lands. Delayed so it doesn't share bandwidth with
  //   wave 1 or with first-click traffic.
  //
  // If a user opens the picker between mount and wave 1 they pay the
  // OF cold cost once — but the relay lane reservation means their
  // call jumps ahead of anything still queued for background.
  useEffect(() => {
    if (accounts.length === 0) return;
    let cancelled = false;
    const BG = { priority: "background" as const };

    // Cheap idle gate so a quick bounce through Inbox doesn't fire the
    // warmup at all. requestIdleCallback (or rAF fallback) waits for the
    // route to actually be settled before scheduling the first wave —
    // prevents the prefetch fan-out from racing a same-tick navigate-away
    // to Home, which left Home's render queued behind in-flight vault
    // prefetches and surfaced as the dev "Rendering…" indicator stalling.
    // Wrap in arrows so the WebIDL methods keep their `window` receiver —
    // bare references throw "Illegal invocation" in some browsers.
    type IdleHandle = number;
    type IdleWin = Window & {
      requestIdleCallback?: (cb: () => void, opts?: { timeout: number }) => number;
      cancelIdleCallback?: (h: number) => void;
    };
    const w = window as IdleWin;
    const hasRic = typeof w.requestIdleCallback === "function";
    const ric: (cb: () => void) => IdleHandle = hasRic
      ? (cb) => w.requestIdleCallback!(cb, { timeout: 2000 })
      : (cb) => window.setTimeout(cb, 0) as unknown as IdleHandle;
    const cic: (h: IdleHandle) => void = hasRic && typeof w.cancelIdleCallback === "function"
      ? (h) => w.cancelIdleCallback!(h)
      : (h) => window.clearTimeout(h as unknown as number);

    const warmAccount = async (aid: string) => {
      if (cancelled) return;
      await Promise.all([
        qc.prefetchQuery({
          queryKey: ["vault-lists", aid],
          // Shared paginating fetcher — a bare limit=50 fetch here would
          // poison the picker's cache with a truncated folder list.
          queryFn: () => fetchAllVaultLists({ accountId: aid, ...BG }),
          staleTime: 5 * 60 * 1000,
        }).catch(() => {}),
        qc.prefetchInfiniteQuery({
          queryKey: ["vault-media", aid, "all", null],
          initialPageParam: 0,
          queryFn: () =>
            relay.get(
              "/api/of/v2/vault/media?limit=24&offset=0&type=all",
              { accountId: aid, ...BG },
            ),
          staleTime: 60_000,
        }).catch(() => {}),
      ]);
    };

    const warmWallMedia = async (aid: string) => {
      if (cancelled) return;
      await qc.prefetchQuery({
        queryKey: ["wall-media", aid],
        queryFn: () =>
          relay.get(
            `/admin/vault/wall-media?account_id=${encodeURIComponent(aid)}`,
            BG,
          ),
        staleTime: 60 * 60 * 1000,
      }).catch(() => {});
    };

    // Wave 1 deferred behind an idle callback chained off a longer timeout.
    // Was 800ms — bumped to 1500ms because a user who clicks Inbox and
    // immediately clicks Home (or any tab) within a second shouldn't pay
    // the prefetch cost at all. The idle wait then yields another frame
    // so we don't slam the network mid-route-transition.
    let r1: IdleHandle | null = null;
    let r2: IdleHandle | null = null;
    const t1 = window.setTimeout(() => {
      if (cancelled) return;
      r1 = ric(() => {
        if (cancelled) return;
        void Promise.all(accounts.map((a) => warmAccount(a.id)));
      });
    }, 1500);

    const t2 = window.setTimeout(() => {
      if (cancelled) return;
      r2 = ric(() => {
        if (cancelled) return;
        void Promise.all(accounts.map((a) => warmWallMedia(a.id)));
      });
    }, 15_000);

    return () => {
      cancelled = true;
      window.clearTimeout(t1);
      window.clearTimeout(t2);
      if (r1 != null) cic(r1);
      if (r2 != null) cic(r2);
    };
  }, [accounts, qc]);

  // Below lg (1024px): single-pane flow. The AccountRoster is hidden
  // (scope switching is reachable from TopNav), and the chat list ↔ chat
  // surface swap is driven by `selected`. lg+ keeps the three-column
  // grid. The lg breakpoint instead of md is intentional — landscape
  // phone (~956px) was too cramped at 3 columns, so it also gets the
  // single-pane treatment.
  return (
    <div
      className="h-chat lg:grid lg:grid-cols-[var(--roster-w)_320px_minmax(0,1fr)] overflow-hidden"
      style={{
        ["--roster-w" as string]: rosterExpanded ? ROSTER_W_EXPANDED : ROSTER_W_COLLAPSED,
      }}
    >
      <div className="hidden lg:block min-h-0">
        <AccountRoster
          expanded={rosterExpanded}
          onToggleExpanded={setRosterExpanded}
        />
      </div>
      <div className={cn(
        "min-h-0 h-full lg:block",
        selected ? "hidden" : "block",
      )}>
        <ChatList selected={selected} onSelect={setSelected} />
      </div>
      {selected ? (
        <ChatSurface
          key={`${selected.accountId}:${selected.fanId}`}
          accountId={selected.accountId}
          fanId={selected.fanId}
          chat={selected.chat}
          onBack={() => setSelected(null)}
        />
      ) : (
        <div className="hidden lg:grid place-items-center h-full text-sm text-fg-dim">
          Pick a conversation on the left.
        </div>
      )}
    </div>
  );
}
