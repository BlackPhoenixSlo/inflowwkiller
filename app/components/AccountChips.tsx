"use client";

/**
 * AccountChips — the shared single-select account picker: one chip per
 * session-backed account, the current one filled. Renders nothing when there
 * is only one account, since there is nothing to pick.
 *
 * Top-level rather than under a feature folder on purpose: every feature
 * surface that is scoped to one model needs it, so it must not live in a
 * module that drags a route's worth of code along with it. It used to be
 * exported from `automations/ReadyMadePanel`, which put that panel's twelve
 * automation tabs — ~331 KB — into the /vault graph for the sake of 31 lines
 * of chips. Import it from here, never re-export it from a feature module.
 */

import { cn } from "@/lib/utils";
import { useActiveAccounts } from "@/hooks/useAccounts";

export function AccountChips({
  accountId,
  onChange,
  className,
}: {
  accountId: string | null;
  onChange: (id: string) => void;
  /** Spacing owned by the call site — the chips carry none of their own. */
  className?: string;
}) {
  const accounts = useActiveAccounts();
  if (accounts.length <= 1) return null;
  return (
    <div className={cn("flex items-center gap-1.5 flex-wrap", className)}>
      <span className="text-[10px] uppercase tracking-wide text-fg-dim mr-1">Account</span>
      {accounts.map((a) => {
        const active = accountId === a.id;
        return (
          <button
            key={a.id}
            type="button"
            onClick={() => onChange(a.id)}
            className={cn(
              "px-2.5 py-1 rounded-full text-xs border transition-colors",
              active
                ? "bg-accent text-white border-accent"
                : "bg-bg-elev-1 text-fg-dim border-border hover:text-fg hover:border-fg-dim",
            )}
          >
            {a.nickname || a.id}
          </button>
        );
      })}
    </div>
  );
}
