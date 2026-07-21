"use client";

/**
 * TopTippersCard — right-rail leaderboard, visible on BOTH the PPV and
 * Tips tabs. Reads /admin/top-tippers; clicks bind the parent page's
 * fan_query filter so the list below filters to that fan.
 *
 * Single-page query (server caps at limit=100, we use 20). Window-focus
 * refetch is ON via the hook — a leaderboard feels live without manual
 * refresh.
 */

import { Card } from "@/components/ui/primitives";
import { fmtCents } from "@/lib/messageFormat";
import { fmtRelTime } from "@/lib/utils";
import {
  useTopTippers,
  type UseTopTippersParams,
} from "@/hooks/useTopTippers";

interface Props extends UseTopTippersParams {
  /** Called when the user clicks a row. Parent should set its fan_query
   *  to the chosen username so the list below filters. */
  onSelectFan?: (username: string) => void;
  /** Free-form label shown under the title (e.g. "last 30 days"). */
  windowLabel?: string;
}

export default function TopTippersCard({
  onSelectFan,
  windowLabel,
  ...params
}: Props) {
  const q = useTopTippers(params);
  const rows = q.data?.rows ?? [];

  return (
    <Card className="p-0 overflow-hidden">
      <header className="p-3 border-b border-border">
        <h2 className="text-sm font-medium text-fg">Top Tippers</h2>
        {windowLabel && (
          <p className="text-[11px] text-fg-dim mt-0.5">{windowLabel}</p>
        )}
      </header>

      {q.isError ? (
        <div className="p-3 text-xs text-err">
          {(q.error as Error)?.message || "Failed to load"}
        </div>
      ) : q.isLoading ? (
        <div className="p-3 text-xs text-fg-dim">Loading…</div>
      ) : rows.length === 0 ? (
        <div className="p-4 text-xs text-fg-dim text-center">
          No tips in this window yet.
        </div>
      ) : (
        <ul className="divide-y divide-border max-h-56 overflow-y-auto overscroll-contain md:max-h-none md:overflow-y-visible">
          {rows.map((r) => {
            const username = r.fan.username;
            const clickable = !!username && !!onSelectFan;
            return (
              <li key={`${r.account_id}:${r.fan_id}`}>
                <button
                  type="button"
                  disabled={!clickable}
                  onClick={() => username && onSelectFan?.(username)}
                  className={
                    "w-full text-left px-3 py-3 md:py-2 flex items-center gap-2 text-sm md:text-xs" +
                    (clickable
                      ? " hover:bg-bg-elev-1/40 cursor-pointer"
                      : " cursor-default")
                  }
                  title={
                    clickable ? `Filter list to @${username}` : undefined
                  }
                >
                  <span className="w-5 tabular-nums text-fg-dim">
                    {r.rank}.
                  </span>
                  <span className="flex-1 min-w-0">
                    <span className="block truncate text-fg">
                      @{username || `fan-${r.fan_id}`}
                    </span>
                    <span className="block md:hidden text-[11px] text-fg-dim truncate">
                      {r.tip_count} tip{r.tip_count === 1 ? "" : "s"} ·{" "}
                      {fmtRelTime(r.last_tip_at)}
                    </span>
                  </span>
                  <span className="tabular-nums font-medium text-fg shrink-0">
                    {fmtCents(r.total_cents)}
                  </span>
                  <span className="hidden md:inline text-fg-dim whitespace-nowrap">
                    ({r.tip_count} tip{r.tip_count === 1 ? "" : "s"} ·{" "}
                    {fmtRelTime(r.last_tip_at)})
                  </span>
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </Card>
  );
}
