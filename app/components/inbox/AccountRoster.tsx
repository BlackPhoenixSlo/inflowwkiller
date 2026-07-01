"use client";

/**
 * AccountRoster — vertical icon strip on the far-left of /inbox.
 *
 * Replaces the implicit "use the TopNav dropdown to switch model"
 * affordance with a one-click vertical column of model avatars.
 * Active model floats to the top; the rest follow in `last_used_at`
 * desc order so the most-recently-touched account is one click away.
 *
 * Click an icon → `setScope({kind:"model", accountId})`. Click the
 * expand button → widens the strip and shells `<AccountRosterExpanded />`
 * for a fan-name peek per account.
 *
 * Width is driven by the parent grid via the `--roster-w` CSS variable:
 * 44px collapsed, 260px expanded. Reorder is purely client-side so the
 * swap renders in one tick — no awaiting, no fetch, no SSE handshake.
 */

import { useCallback, useEffect, useMemo, useRef } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { ChevronRight, ChevronLeft } from "lucide-react";

import { useActiveAccounts } from "@/hooks/useAccounts";
import { useAllModelsInclude } from "@/hooks/useAllModelsInclude";
import { prefetchModelChatList } from "@/hooks/useChatList";
import {
  useRosterCounts,
  useRosterCountActions,
  type RosterCount,
} from "@/hooks/useRosterCounts";
import { useScope } from "@/contexts/ScopeContext";
import { cn } from "@/lib/utils";
import type { AccountMeta } from "@/lib/relay";

import { AccountRosterIcon, AllModelsRosterIcon } from "./AccountRosterIcon";
import { AccountRosterExpanded } from "./AccountRosterExpanded";

export interface AccountRosterProps {
  expanded: boolean;
  onToggleExpanded: (next: boolean) => void;
}

function compareAccounts(a: AccountMeta, b: AccountMeta): number {
  const ta = a.last_used_at ?? "";
  const tb = b.last_used_at ?? "";
  if (ta && tb && ta !== tb) return tb.localeCompare(ta);
  if (ta && !tb) return -1;
  if (!ta && tb) return 1;
  return (a.nickname || a.id).localeCompare(b.nickname || b.id);
}

export function AccountRoster({ expanded, onToggleExpanded }: AccountRosterProps) {
  const accounts = useActiveAccounts();
  const { scope, setScope } = useScope();
  const { isIncluded } = useAllModelsInclude();
  const counts = useRosterCounts();
  const { refreshOne } = useRosterCountActions();
  const qc = useQueryClient();

  // Prewarm a model the instant it's touched: force its unread badge fresh from
  // OF (bust the 5-min server cache) AND prefetch its chat list, so a swap paints
  // authoritative + spinner-free instead of waiting on the 60s poll. `refreshOne`
  // is per-account cooldown-guarded, so hover→click doesn't double-read OF.
  const warm = useCallback(
    (accountId: string) => {
      void refreshOne(accountId);
      void prefetchModelChatList(qc, accountId);
    },
    [refreshOne, qc],
  );

  // Hover intent: only warm after the pointer settles (250ms) so sweeping the
  // mouse down the strip doesn't fire a live OF read per icon it crosses.
  const hoverTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const clearHover = useCallback(() => {
    if (hoverTimerRef.current) {
      clearTimeout(hoverTimerRef.current);
      hoverTimerRef.current = null;
    }
  }, []);
  const onHoverModel = useCallback(
    (accountId: string) => {
      clearHover();
      hoverTimerRef.current = setTimeout(() => warm(accountId), 250);
    },
    [clearHover, warm],
  );
  useEffect(() => clearHover, [clearHover]);

  // Swap: reorder is client-side/instant; also warm so the just-picked model's
  // badge + list are fresh. Cooldown means a prior hover already covered it.
  const onPickModel = useCallback(
    (accountId: string) => {
      clearHover();
      setScope({ kind: "model", accountId });
      warm(accountId);
    },
    [clearHover, setScope, warm],
  );

  // Active first, rest by last_used_at desc. Excluded models (unchecked
  // in the All-Models aggregate via ScopeSwitcher) are hidden — unconditionally,
  // so the rule is one-line: "uncheck in the top picker → gone from the
  // roster." The top picker still lists everything, so re-including is a
  // one-click trip up there. If the active scope itself is excluded, its
  // chats still load and the top picker still names it; the roster just
  // doesn't draw a row for it.
  const ordered = useMemo(() => {
    const activeId = scope.kind === "model" ? scope.accountId : null;
    const visible = accounts.filter((a) => isIncluded(a.id));
    const active = activeId ? visible.find((a) => a.id === activeId) : null;
    const rest = visible
      .filter((a) => a.id !== activeId)
      .slice()
      .sort(compareAccounts);
    return active ? [active, ...rest] : rest;
  }, [accounts, scope, isIncluded]);

  // Aggregate badge for the "all models" icon = sum across the visible models.
  const allCount = useMemo<RosterCount>(() => {
    return ordered.reduce(
      (acc, a) => {
        const c = counts[a.id];
        if (c) { acc.unread += c.unread || 0; acc.owe_reply += c.owe_reply || 0; }
        return acc;
      },
      { unread: 0, owe_reply: 0 },
    );
  }, [ordered, counts]);

  // The "all models" aggregate badge is the SUM of the per-model counts, so it's
  // only as fresh as those are — and clicking it just switches scope, nothing
  // refreshes the underlying models. Refresh EVERY visible model so the sum goes
  // fresh. Each refreshOne is per-account cooldown-guarded, so this can't storm OF
  // on repeated clicks (and a model just refreshed via its own icon is skipped).
  // Defined AFTER `ordered` so the dep isn't read in its temporal dead zone.
  const warmAll = useCallback(() => {
    for (const a of ordered) void refreshOne(a.id);
  }, [ordered, refreshOne]);
  const onPickAll = useCallback(() => {
    clearHover();
    setScope({ kind: "all" });
    warmAll();
  }, [clearHover, setScope, warmAll]);
  const onHoverAll = useCallback(() => {
    clearHover();
    hoverTimerRef.current = setTimeout(() => warmAll(), 250);
  }, [clearHover, warmAll]);

  if (accounts.length === 0) {
    // No session-bearing accounts → no strip. Inbox still renders its
    // existing "no accounts" path through ChatList.
    return <div aria-hidden />;
  }

  return (
    <div
      className={cn(
        "h-full flex flex-col border-r border-border bg-bg-elev-0/60",
        "transition-[width] duration-150 overflow-hidden",
      )}
    >
      <button
        type="button"
        onClick={() => onToggleExpanded(!expanded)}
        title={expanded ? "Collapse roster" : "Expand roster"}
        aria-expanded={expanded}
        className={cn(
          "shrink-0 flex items-center justify-center",
          "h-10 border-b border-border text-fg-dim hover:text-fg hover:bg-bg-elev-1",
          expanded ? "px-3 gap-2 justify-between" : "w-full",
        )}
      >
        {expanded ? (
          <>
            <span className="text-[11px] uppercase tracking-wide">Models</span>
            <ChevronLeft className="w-4 h-4" />
          </>
        ) : (
          <ChevronRight className="w-4 h-4" />
        )}
      </button>

      {expanded ? (
        <AccountRosterExpanded
          accounts={ordered}
          activeAccountId={scope.kind === "model" ? scope.accountId : null}
          isIncluded={isIncluded}
          counts={counts}
          allCount={allCount}
          onPick={onPickModel}
          onHover={onHoverModel}
          onHoverEnd={clearHover}
          onPickAll={onPickAll}
          onHoverAll={onHoverAll}
          allActive={scope.kind === "all"}
        />
      ) : (
        <div className="flex-1 min-h-0 overflow-y-auto py-2 flex flex-col items-center gap-2">
          {scope.kind === "all" && (
            <AllModelsRosterIcon
              active
              count={allCount}
              onClick={onPickAll}
              onHover={onHoverAll}
              onHoverEnd={clearHover}
            />
          )}
          {ordered.map((a) => {
            const active = scope.kind === "model" && scope.accountId === a.id;
            return (
              <AccountRosterIcon
                key={a.id}
                account={a}
                active={active}
                count={counts[a.id]}
                onClick={() => onPickModel(a.id)}
                onHover={() => onHoverModel(a.id)}
                onHoverEnd={clearHover}
              />
            );
          })}
          {scope.kind !== "all" && (
            <AllModelsRosterIcon
              active={false}
              count={allCount}
              onClick={onPickAll}
              onHover={onHoverAll}
              onHoverEnd={clearHover}
            />
          )}
        </div>
      )}
    </div>
  );
}
