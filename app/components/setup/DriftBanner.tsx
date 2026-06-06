"use client";

/**
 * DriftBanner — shown at the top of /setup when at least one account's
 * stored x-of-rev no longer matches the live OF build hash.
 *
 * Mirrors the existing /ui/ drift banner but as a React component:
 *   • Polls /admin/rev/drift every 5 minutes (per the plan).
 *   • Dismissible per-rev — dismissing on rev A doesn't suppress the
 *     warning when rev B ships and a new drift appears.
 *   • Lists which accounts are stale with their nicknames so you know
 *     exactly which model needs re-capture.
 */

import { useQuery } from "@tanstack/react-query";
import { useState, useEffect } from "react";

import { relay, type DriftReport } from "@/lib/relay";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/primitives";

const DISMISS_LS_KEY = "chatterly:drift_dismissed_for";

export default function DriftBanner() {
  const [dismissedFor, setDismissedFor] = useState<string | null>(null);

  // Hydrate dismissal state from localStorage. Effect so SSR is happy.
  useEffect(() => {
    try {
      setDismissedFor(window.localStorage.getItem(DISMISS_LS_KEY));
    } catch { /* ignore */ }
  }, []);

  const drift = useQuery<DriftReport>({
    queryKey: ["drift"],
    queryFn: () => relay.get<DriftReport>("/admin/rev/drift"),
    refetchInterval: 5 * 60_000,
    staleTime: 60_000,
  });

  if (!drift.data?.any_drift) return null;
  if (drift.data.live_rev && dismissedFor === drift.data.live_rev) return null;

  const stale = drift.data.accounts.filter((a) => a.drift);
  const labels = stale.map((a) => a.nickname || a.account_id).join(", ");

  const dismiss = () => {
    if (drift.data?.live_rev) {
      try {
        window.localStorage.setItem(DISMISS_LS_KEY, drift.data.live_rev);
      } catch { /* ignore */ }
      setDismissedFor(drift.data.live_rev);
    }
  };

  return (
    <div className={cn(
      "mb-4 p-4 rounded-xl border",
      "bg-warn/10 border-warn/40 text-warn",
    )}>
      <div className="flex items-start gap-3">
        <span className="text-xl leading-none">⚠</span>
        <div className="flex-1">
          <div className="font-semibold text-fg mb-1">
            Session stale — re-capture required
          </div>
          <p className="text-sm text-fg/90">
            OnlyFans shipped a new frontend (rev <code className="text-xs">{drift.data.live_rev}</code>).
            Stale: <strong>{labels}</strong>. Sign in again via the Fastt Login
            extension and paste the fresh cURL below.
          </p>
        </div>
        <Button variant="ghost" size="sm" onClick={dismiss}>
          Dismiss
        </Button>
      </div>
    </div>
  );
}
