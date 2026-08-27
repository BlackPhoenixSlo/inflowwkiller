"use client";

/**
 * LaneConfigCard — set the relay's concurrency ceilings from the browser.
 *
 * ⚠️ THIS REVERSES A DELIBERATE EARLIER DECISION, and the reasoning it reverses
 * is still true. LanesTab used to say a cap "is changed at boot, where a bad
 * number costs a restart rather than an outage", because these ceilings are what
 * stopped the 2026-08-08 descriptor exhaustion from recurring and a control that
 * widens them can reproduce it. What changed is the judgement: the ceilings had
 * never once been retuned, because retuning meant editing an env file the deploy
 * rsync excludes. A ceiling nobody can change is a ceiling nobody maintains.
 *
 * The safety property is preserved rather than dropped. A saved value applies at
 * RESTART, so a bad number still costs a restart, not an outage — which is why
 * this card is emphatic about "restart to apply" instead of hiding it. And an
 * env var still beats a saved value, so a lockout is recoverable without anyone
 * having to find a JSON file inside a container.
 *
 * The observability tables below this card are what tell you whether a number is
 * worth changing at all. A cap sitting at its default says nothing; a `peak`
 * touching the cap, or a `rejected` climbing, is the evidence. Read those first.
 */

import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { Card } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";
import { relay } from "@/lib/relay";

interface Knob {
  name: string;
  /** What the store + env resolve to — i.e. what the NEXT restart will use. */
  value: number;
  default: number;
  stored: number | null;
  source: "env" | "stored" | "default";
  env_locked: boolean;
  /** What the running process is enforcing right now. */
  live: number | null;
  /** Saved but not yet adopted — the whole reason this card shows two numbers. */
  pending: boolean;
}

interface LaneConfigResp {
  knobs: Knob[];
  restart_required: boolean;
  path: string;
}

/** Presentation only: ordering and the copy that explains each family. The
 *  server is the source of truth for WHICH knobs exist — anything it sends that
 *  is not named here still renders, under "Other", rather than silently
 *  vanishing from the panel. A knob you cannot see is a knob you cannot fix. */
const GROUPS: { title: string; blurb: string; names: string[] }[] = [
  {
    title: "Automation throughput",
    blurb:
      "Measured the ceiling that binds first as accounts are added — at 7 accounts this ran 23.5% utilized, so it saturates near 30. Bulk sweeps get their own slots so they can never starve a real-time reply.",
    names: ["AUTOMATION_MAX_CONCURRENT_RUNS", "AUTOMATION_MAX_CONCURRENT_BULK"],
  },
  {
    title: "Shared thread pool",
    blurb:
      "Every OnlyFans call in the process funnels through this one pool. It is the budget the per-account lane spends from: accounts × per-account cap should stay well under it, or work queues here instead — invisibly, with no counter and nothing turned away.",
    names: ["RELAY_EXECUTOR_THREADS"],
  },
  {
    title: "Per-account OnlyFans lane",
    blurb:
      "Per account, not shared — fleet-wide concurrency is this × live accounts. Background is a sub-cap of total, so it is clamped below it no matter what you type.",
    names: ["ACCOUNT_LANE_TOTAL", "ACCOUNT_LANE_BACKGROUND"],
  },
  {
    title: "Media and vault lanes",
    blurb:
      "These guard file descriptors and CPU. img fetch is the one that stopped a vault pane from emptying the descriptor table; storyboard build is ffmpeg, so it scales with cores, not accounts.",
    names: [
      "IMG_FETCH_CONCURRENCY",
      "VIDEO_STREAM_CONCURRENCY",
      "STORYBOARD_BUILD_CONCURRENCY",
      "VAULT_MEDIA_CONCURRENCY",
      "VAULT_STILL_CONCURRENCY",
    ],
  },
  {
    title: "Health probe",
    blurb:
      "The /setup table's per-account OnlyFans probe. Size it above the number of accounts that can be slow at once, not off how fast a healthy probe is — dead sessions hold a slot for the full timeout.",
    names: ["HEALTH_ALL_CONCURRENCY"],
  },
];

function pretty(name: string): string {
  return name
    .replace(/^AUTOMATION_MAX_CONCURRENT_/, "")
    .replace(/_CONCURRENCY$/, "")
    .replace(/^RELAY_/, "")
    .replace(/^ACCOUNT_LANE_/, "account ")
    .replace(/_/g, " ")
    .toLowerCase();
}

export default function LaneConfigCard() {
  const qc = useQueryClient();
  const q = useQuery<LaneConfigResp>({
    queryKey: ["admin", "lane-config"],
    queryFn: () => relay.get<LaneConfigResp>("/admin/lane-config"),
    staleTime: 0,
  });

  /** Local edits, keyed by knob. Absent = untouched. Kept as strings so a
   *  half-typed "1" on the way to "16" does not get coerced to a cap of 1. */
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [err, setErr] = useState<string | null>(null);

  const save = useMutation({
    mutationFn: (values: Record<string, number | null>) =>
      relay.post<LaneConfigResp>("/admin/lane-config", { values }),
    onSuccess: (data) => {
      setErr(null);
      // Server state wins after a save — a clamped value (background below
      // total) would otherwise keep showing the number that was typed rather
      // than the one that was stored. Cleared HERE and not in an effect on
      // `q.data`: a refetch must never wipe what someone is halfway through
      // typing, and an effect keyed on the query would do exactly that.
      setDraft({});
      qc.setQueryData(["admin", "lane-config"], data);
      // The lanes table shows live caps; they have not moved, but the tab's
      // copy keys off this query too.
      qc.invalidateQueries({ queryKey: ["admin", "lanes"] });
    },
    onError: (e: unknown) => setErr((e as Error)?.message || "Save failed"),
  });

  const byName = useMemo(() => {
    const m = new Map<string, Knob>();
    for (const k of q.data?.knobs ?? []) m.set(k.name, k);
    return m;
  }, [q.data]);

  /** GROUPS, plus a trailing catch-all for anything the server knows about and
   *  this file does not. Derived rather than hand-maintained: adding a knob to
   *  the relay must never require an edit here to make it visible. */
  const sections = useMemo(() => {
    const named = new Set(GROUPS.flatMap((g) => g.names));
    const rest = (q.data?.knobs ?? [])
      .map((k) => k.name)
      .filter((n) => !named.has(n));
    return rest.length
      ? [...GROUPS, { title: "Other", blurb: "Ceilings this panel has no copy for yet.", names: rest }]
      : GROUPS;
  }, [q.data]);

  const dirty = useMemo(
    () =>
      Object.entries(draft).filter(([name, raw]) => {
        const k = byName.get(name);
        return k != null && raw.trim() !== "" && Number(raw) !== k.value;
      }),
    [draft, byName],
  );

  if (q.isLoading) return <div className="text-sm text-fg-dim">Loading…</div>;
  if (q.isError) {
    return (
      <Card className="p-4">
        <p className="text-sm text-err">
          {(q.error as Error)?.message || "Couldn't read the ceilings"}
        </p>
      </Card>
    );
  }

  const d = q.data!;
  // Only the COUNT is derived here. Whether a restart is needed at all stays
  // the server's answer (`restart_required`) — "saved but not running" gets one
  // definition, and it lives next to the state it describes.
  const pendingCount = d.knobs.filter((k) => k.pending).length;

  function onSave() {
    const values: Record<string, number | null> = {};
    for (const [name, raw] of dirty) values[name] = Number(raw);
    if (Object.keys(values).length) save.mutate(values);
  }

  return (
    <Card className="p-4 space-y-4">
      <header className="space-y-1">
        <h2 className="text-sm font-medium text-fg">Set the ceilings</h2>
        <p className="text-xs text-fg-dim max-w-2xl">
          Saved here and applied at the <strong>next relay restart</strong> —
          never live. Every ceiling is a semaphore built once at startup, so a
          value that changed the number on this screen without changing the one
          being enforced would be worse than no control at all.
        </p>
      </header>

      {d.restart_required && (
        <div className="rounded border border-warn/40 bg-warn/10 px-3 py-2 text-xs text-warn">
          <strong>Restart to apply.</strong> {pendingCount} ceiling
          {pendingCount === 1 ? " is" : "s are"} saved but not yet running. The
          relay is still enforcing the old numbers.
        </div>
      )}

      {err && (
        <div className="rounded border border-err/40 bg-err/10 px-3 py-2 text-xs text-err">
          {err}
        </div>
      )}

      <div className="space-y-5">
        {sections.map((g) => (
          <section key={g.title} className="space-y-2">
            <div>
              <h3 className="text-xs font-medium text-fg">{g.title}</h3>
              <p className="text-[11px] text-fg-dim max-w-2xl">{g.blurb}</p>
            </div>
            <div className="space-y-1.5">
              {g.names.map((name) => {
                const k = byName.get(name);
                if (!k) return null;
                const raw = draft[name] ?? String(k.value);
                return (
                  <div
                    key={name}
                    className="flex flex-wrap items-center gap-x-3 gap-y-1 border-t border-border pt-1.5"
                  >
                    <label
                      htmlFor={`knob-${name}`}
                      className="min-w-[168px] text-sm text-fg"
                    >
                      {pretty(name)}
                      <span className="block font-mono text-[10px] text-fg-dim">
                        {name}
                      </span>
                    </label>

                    <input
                      id={`knob-${name}`}
                      type="number"
                      min={1}
                      inputMode="numeric"
                      disabled={k.env_locked}
                      value={raw}
                      onChange={(e) =>
                        setDraft((p) => ({ ...p, [name]: e.target.value }))
                      }
                      className={cn(
                        "w-20 rounded border border-border bg-bg-elev-1 px-2 py-1 text-sm tabular-nums",
                        k.env_locked && "opacity-50 cursor-not-allowed",
                      )}
                    />

                    <span className="text-[11px] text-fg-dim">
                      default {k.default}
                    </span>

                    {k.env_locked && (
                      <span className="rounded bg-bg-elev-1 px-1.5 py-0.5 text-[10px] text-fg-dim">
                        pinned by env var — saving here would not take effect
                      </span>
                    )}

                    {k.pending && (
                      <span className="rounded bg-warn/15 px-1.5 py-0.5 text-[10px] text-warn">
                        running {k.live}, will be {k.value} after restart
                      </span>
                    )}

                    {k.stored != null && !k.env_locked && (
                      <button
                        type="button"
                        onClick={() => save.mutate({ [name]: null })}
                        className="text-[11px] text-fg-dim underline hover:text-fg"
                      >
                        reset
                      </button>
                    )}
                  </div>
                );
              })}
            </div>
          </section>
        ))}
      </div>

      <div className="flex flex-wrap items-center gap-3 border-t border-border pt-3">
        <button
          type="button"
          onClick={onSave}
          disabled={dirty.length === 0 || save.isPending}
          className={cn(
            "rounded px-3 py-1.5 text-sm font-medium",
            dirty.length === 0 || save.isPending
              ? "bg-bg-elev-1 text-fg-dim cursor-not-allowed"
              : "bg-accent text-white hover:opacity-90",
          )}
        >
          {save.isPending
            ? "Saving…"
            : dirty.length === 0
              ? "No changes"
              : `Save ${dirty.length} change${dirty.length === 1 ? "" : "s"}`}
        </button>
        <span className="font-mono text-[10px] text-fg-dim">{d.path}</span>
      </div>
    </Card>
  );
}
