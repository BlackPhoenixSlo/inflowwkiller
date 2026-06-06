"use client";

/**
 * Home — Phase A.8 ships this as a thin "you're in!" landing page that
 * proves the full chain (employee picker → providers → relay → DB).
 *
 * Renders the current employee + a live status pill that reads /health.
 * Phase B replaces this with the Dashboard component (ported from the
 * desktop-app's Dashboard.tsx) once the data layer is fully wired.
 */

import dynamic from "next/dynamic";
import { useQuery } from "@tanstack/react-query";

import { useEmployee } from "@/contexts/EmployeeContext";
import { useScope } from "@/contexts/ScopeContext";
import { relay } from "@/lib/relay";

// CompetitorReports is ~1300 lines and pulls in usePerModel (which has
// refetchOnMount: "always") + 2x useQueries fanning out one /admin/stats
// /revenue request per model account. Statically importing it bloated
// Home's initial chunk and made mounts heavy — directly navigating
// Inbox → Home stalled behind the chart's mount work + queries, surfacing
// as the dev "Rendering…" indicator hanging for ~1s. Going Inbox →
// Messages → Home felt instant only because the Messages detour let
// Inbox's background warmup drain first.
//
// `next/dynamic` with ssr:false splits both heavy panels out of the
// initial chunk, so the welcome card + Add-Model button paint
// immediately on route commit. The charts hydrate asynchronously below.
const TotalRevenueChart = dynamic(
  () => import("@/components/home/TotalRevenueChart"),
  { ssr: false, loading: () => <ChartSkeleton h={160} /> },
);
const CompetitorReports = dynamic(
  () => import("@/components/home/CompetitorReports"),
  { ssr: false, loading: () => <ChartSkeleton h={400} /> },
);

interface HealthResp { ok?: boolean; name?: string; user_id?: number | string; error?: string }

export default function HomePage() {
  const { current, clear } = useEmployee();
  const { scope } = useScope();

  // Hit /health through the rewrite — confirms the relay is reachable
  // and the share-token gate is satisfied.
  const health = useQuery<HealthResp>({
    queryKey: ["health"],
    queryFn: () => relay.get<HealthResp>("/health"),
    refetchInterval: 30_000,
    staleTime: 30_000,
  });

  return (
    <div className="min-h-screen p-4 sm:p-8 max-w-5xl mx-auto">
      {/* Top bar */}
      <div className="flex flex-wrap items-center justify-between gap-3 mb-6 sm:mb-10">
        <div className="flex items-center gap-3">
          <div
            className="w-3 h-3 rounded-full"
            style={{ background: current?.color || "#888" }}
          />
          <span className="text-sm">
            Chatting as <strong>{current?.display_name}</strong>
          </span>
          <button
            type="button"
            onClick={clear}
            className="text-xs text-fg-dim hover:text-fg underline underline-offset-2"
          >
            switch
          </button>
        </div>

        <div className="flex items-center gap-3">
          <a
            href="/setup"
            className="px-3 py-1.5 rounded-lg text-sm bg-accent text-white hover:bg-accent-hover font-medium"
          >
            + Add Model
          </a>
          <span className="text-xs text-fg-dim">scope:</span>
          <code className="text-xs bg-bg-elev-1 border border-border rounded px-2 py-0.5">
            {scope.kind === "all" ? "all" : scope.accountId}
          </code>
        </div>
      </div>

      {/* Revenue chart — all models, daily totals */}
      <div className="mb-6">
        <TotalRevenueChart />
      </div>

      {/* Competitor "Creator Reports" — joke screenshot block */}
      <div className="mb-6">
        <CompetitorReports />
      </div>

      {/* Welcome card */}
      <div className="bg-panel border border-border rounded-2xl p-5 sm:p-8 mt-10">
        <h1 className="text-2xl font-semibold mb-2">Fastt is live</h1>
        <p className="text-fg-dim mb-6">
          Phase A complete: DB foundation, SSE pipeline, employee picker, audit log.
          The Next.js shell is up and talking to the Python relay through rewrites.
        </p>

        <div className="grid grid-cols-2 gap-3">
          <Pill
            label="Relay /health"
            value={
              health.isLoading ? "…" :
              health.error ? "down" :
              health.data?.ok ? `ok · ${health.data?.name ?? "no model"}` : "no session"
            }
            ok={!!health.data?.ok}
            err={!!health.error}
          />
          <Pill
            label="DB"
            value="SQLite · 35 tables"
            ok
          />
        </div>
      </div>

    </div>
  );
}

function ChartSkeleton({ h }: { h: number }) {
  return (
    <div
      className="bg-panel border border-border rounded-2xl animate-pulse"
      style={{ height: h }}
    />
  );
}

function Pill({ label, value, ok, err }: {
  label: string; value: string; ok?: boolean; err?: boolean;
}) {
  return (
    <div className="bg-bg-elev-1 border border-border rounded-xl p-4">
      <div className="text-xs uppercase tracking-wider text-muted mb-1">{label}</div>
      <div className={
        err ? "text-err text-sm" :
        ok ? "text-ok text-sm font-medium" :
        "text-warn text-sm"
      }>
        {value}
      </div>
    </div>
  );
}
