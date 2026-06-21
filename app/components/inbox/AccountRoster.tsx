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

import { useMemo } from "react";
import { ChevronRight, ChevronLeft } from "lucide-react";

import { useActiveAccounts } from "@/hooks/useAccounts";
import { useAllModelsInclude } from "@/hooks/useAllModelsInclude";
import { useRosterCounts, type RosterCount } from "@/hooks/useRosterCounts";
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
          onPick={(accountId) => setScope({ kind: "model", accountId })}
          onPickAll={() => setScope({ kind: "all" })}
          allActive={scope.kind === "all"}
        />
      ) : (
        <div className="flex-1 min-h-0 overflow-y-auto py-2 flex flex-col items-center gap-2">
          {scope.kind === "all" && (
            <AllModelsRosterIcon
              active
              count={allCount}
              onClick={() => setScope({ kind: "all" })}
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
                onClick={() => setScope({ kind: "model", accountId: a.id })}
              />
            );
          })}
          {scope.kind !== "all" && (
            <AllModelsRosterIcon
              active={false}
              count={allCount}
              onClick={() => setScope({ kind: "all" })}
            />
          )}
        </div>
      )}
    </div>
  );
}
