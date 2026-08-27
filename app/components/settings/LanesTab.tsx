"use client";

/**
 * LanesTab — every concurrency ceiling in the relay, in one read.
 *
 * THE CAPS ARE NOW SETTABLE — see LaneConfigCard, which is the card above these
 * tables and carries the reasoning for reversing that. These tables stay
 * READ-ONLY on purpose: they are the evidence, not the control. Their job is the
 * question you cannot answer from a cap's value — is a ceiling actually binding?
 *
 * What this tab is FOR is the question you cannot answer without it — is a
 * ceiling actually binding? A cap sitting at its default tells you nothing; a
 * `peak` that has touched the cap, or a `rejected` that is climbing, tells you
 * the lane is the thing making people wait. That is how the /health probe cap
 * was found to be the reason a slow account could stall /setup for 20s.
 *
 * The two families answer different questions and are deliberately NOT merged
 * into one table:
 *   • process lanes — one ceiling shared by every account.
 *   • the per-account OF lane — one ceiling PER account, so fleet-wide
 *     concurrency is (cap x live accounts), not cap. Reading it as a single
 *     number is the mistake this split exists to prevent.
 */

import { useQuery } from "@tanstack/react-query";

import { Card } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";
import { relay } from "@/lib/relay";

import LaneConfigCard from "./LaneConfigCard";

interface LaneRow {
  name: string;
  kind: "thread" | "loop";
  cap: number;
  busy: number;
  rejected: number | null;
  peak: number | null;
  wait_s: number | null;
  env_var: string;
}

interface AccountLaneView {
  calls: number;
  blocked: number;
  blocked_rate: number;
  wait_s_avg_blocked: number;
  wait_s_max: number;
  in_flight: number;
  in_flight_peak: number;
}

interface LanesResp {
  lanes: LaneRow[];
  account_lanes: {
    cap_total: number;
    cap_background: number;
    uptime_s: number;
    overall: AccountLaneView;
    accounts: Record<string, AccountLaneView>;
  };
  account_cap_total: number;
  account_cap_background: number;
  accounts_live: number;
}

/** A cap is "reached" when the high-water mark has touched it. That — not the
 *  cap's absolute value — is the only thing that makes a lane interesting. */
function pressure(row: LaneRow): { label: string; tone: "idle" | "warn" | "hot" } {
  if (row.rejected != null && row.rejected > 0) return { label: "rejecting", tone: "hot" };
  const high = row.kind === "loop" ? row.peak ?? 0 : row.busy;
  if (high >= row.cap) return { label: "at cap", tone: "warn" };
  if (high > 0) return { label: "used", tone: "idle" };
  return { label: "idle", tone: "idle" };
}

function Pill({ tone, children }: { tone: "idle" | "warn" | "hot"; children: React.ReactNode }) {
  return (
    <span
      className={cn(
        "px-1.5 py-0.5 rounded text-[11px] font-medium whitespace-nowrap",
        tone === "hot" && "bg-err/15 text-err",
        tone === "warn" && "bg-warn/15 text-warn",
        tone === "idle" && "bg-bg-elev-1 text-fg-dim",
      )}
    >
      {children}
    </span>
  );
}

export default function LanesTab() {
  const q = useQuery<LanesResp>({
    queryKey: ["admin", "lanes"],
    queryFn: () => relay.get<LanesResp>("/admin/lanes"),
    // Live gauges: busy/in_flight are instantaneous, so a stale read is a
    // misleading one. Short interval, and no persistence.
    refetchInterval: 5_000,
    staleTime: 0,
  });

  if (q.isLoading) return <div className="text-sm text-fg-dim">Loading…</div>;
  if (q.isError) {
    return (
      <div className="text-sm text-err">
        {(q.error as Error)?.message || "Couldn't read the lanes"}
      </div>
    );
  }
  const d = q.data!;
  const acct = d.account_lanes;

  return (
    <div className="flex flex-col gap-5">
      <p className="text-sm text-fg-dim max-w-2xl">
        Every ceiling on how much work the relay will do at once. Set them below;
        each takes effect at the next restart, so a bad value costs a restart
        instead of an outage. What tells you a number is worth changing is not
        the cap itself but whether anything has <em>reached</em> it — which is
        what the tables underneath are for.
      </p>

      <LaneConfigCard />

      <Card className="p-4 space-y-3">
        <header>
          <h2 className="text-sm font-medium text-fg">Process lanes</h2>
          <p className="text-xs text-fg-dim">
            One ceiling each, shared by every account.
          </p>
        </header>

        <div className="overflow-x-auto">
          <table className="w-full text-sm min-w-[620px]">
            <thead>
              <tr className="text-left text-[11px] uppercase tracking-wide text-fg-dim">
                <th className="py-1.5 pr-3 font-medium">Lane</th>
                <th className="py-1.5 pr-3 font-medium">Cap</th>
                <th className="py-1.5 pr-3 font-medium">In use</th>
                <th className="py-1.5 pr-3 font-medium">Peak</th>
                <th className="py-1.5 pr-3 font-medium">Turned away</th>
                <th className="py-1.5 pr-3 font-medium">State</th>
                <th className="py-1.5 font-medium">Set with</th>
              </tr>
            </thead>
            <tbody>
              {d.lanes.map((row) => {
                const p = pressure(row);
                return (
                  <tr key={row.name} className="border-t border-border">
                    <td className="py-2 pr-3">
                      <span className="font-medium">{row.name.replace(/_/g, " ")}</span>
                      <span className="block text-[11px] text-fg-dim">
                        {row.kind === "thread"
                          ? `waits ${row.wait_s}s, then turns the caller away`
                          : "queues — never turns a caller away"}
                      </span>
                    </td>
                    <td className="py-2 pr-3 tabular-nums">{row.cap}</td>
                    <td className="py-2 pr-3 tabular-nums">{row.busy}</td>
                    <td className="py-2 pr-3 tabular-nums text-fg-dim">
                      {row.peak ?? "—"}
                    </td>
                    <td className="py-2 pr-3 tabular-nums">
                      {row.rejected == null ? (
                        <span className="text-fg-dim">n/a</span>
                      ) : (
                        <span className={cn(row.rejected > 0 && "text-err font-medium")}>
                          {row.rejected}
                        </span>
                      )}
                    </td>
                    <td className="py-2 pr-3"><Pill tone={p.tone}>{p.label}</Pill></td>
                    <td className="py-2 font-mono text-[11px] text-fg-dim">{row.env_var}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </Card>

      <Card className="p-4 space-y-3">
        <header>
          <h2 className="text-sm font-medium text-fg">Per-account OnlyFans lane</h2>
          <p className="text-xs text-fg-dim">
            {d.account_cap_total} slots ({d.account_cap_background} reserved for
            background work) <strong>per account</strong> — not shared. With{" "}
            {d.accounts_live} account{d.accounts_live === 1 ? "" : "s"} live that
            is up to {d.account_cap_total * Math.max(d.accounts_live, 1)}{" "}
            OnlyFans calls at once across the fleet.
          </p>
        </header>

        <dl className="grid grid-cols-2 sm:grid-cols-4 gap-3 text-sm">
          {[
            ["Calls", acct.overall.calls],
            ["Queued", acct.overall.blocked],
            ["Queued rate", `${(acct.overall.blocked_rate * 100).toFixed(1)}%`],
            ["Longest wait", `${acct.overall.wait_s_max.toFixed(2)}s`],
          ].map(([label, value]) => (
            <div key={String(label)} className="rounded border border-border p-2">
              <dt className="text-[11px] uppercase tracking-wide text-fg-dim">{label}</dt>
              <dd className="tabular-nums text-fg">{value}</dd>
            </div>
          ))}
        </dl>

        <p className="text-xs text-fg-dim">
          A queued rate near zero means the per-account cap is not what anyone is
          waiting on. This lane is deliberately unbounded — a caller waits rather
          than being turned away — so these numbers are the evidence for whether
          it ever should be.
        </p>
      </Card>
    </div>
  );
}
