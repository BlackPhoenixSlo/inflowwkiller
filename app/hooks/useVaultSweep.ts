"use client";

/**
 * useVaultSweep — the one client-side model of a vault background sweep.
 *
 * The server runs several of these and three are literally the same object: a
 * module-level `_X_running: set[str]`, a `_X_progress` counter dict, a 409 on
 * double-start, and `GET /admin/vault-ai/<kind>/status` → `{running, progress}`.
 * `describe-all`, `flags-all` and `harvest-keywords` differ only in which keys
 * they put in `progress`.
 *
 * The client used to model that three different ways: one polled query
 * (`useVaultFlagsStatus`) and two hand-rolled `for (i < N) { sleep; fetch }`
 * loops inside the click handlers. The loops were the same bug twice over —
 * progress lived only in the tab that pressed the button, so it died on reload,
 * never existed in a second tab, and gave up after a fixed tick count (17 min for
 * describe, 7 for harvest) while a 500-item describe pass runs for over an hour.
 * Past that point the button read "Describe all (N)" over a sweep that was barely
 * a third done, and the operator's only remaining feedback was to press it again
 * — which 409s, which the handler swallowed, which put the label straight back.
 *
 * Asking the server instead makes the answer true for any mount, any tab, for
 * exactly as long as the sweep runs.
 *
 * WHAT LIVES HERE AND WHAT DOES NOT
 * ---------------------------------
 * The hook owns polling, the start call, the running→idle edge, and refreshing
 * whatever the sweep writes underneath the page. It does NOT own what a button
 * says: the label formatters below are pure functions of a progress dict, so
 * they are readable and testable without a DOM.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import {
  useQuery,
  useQueryClient,
  type QueryKey,
  type UseQueryResult,
} from "@tanstack/react-query";

import { relay } from "@/lib/relay";

/** The sweeps that share the `{running, progress}` contract. This is also the
 *  URL segment: start is POST `/admin/vault-ai/<kind>`, status is GET
 *  `/admin/vault-ai/<kind>/status`. */
export type SweepKind = "describe-all" | "flags-all" | "harvest-keywords";

/** The union of what the sweeps count. Everything past `total`/`done` is
 *  per-sweep AND optional, because the server fills the dict in stages:
 *  `{total, done}` at kickoff, `failed`/`error` as items fail, `needs_review`
 *  only once the pass lands. */
export interface SweepProgress {
  total: number;
  /** Items VISITED, not items that succeeded — a failure counts here too. */
  done: number;
  capped?: boolean;
  failed?: number;
  error?: string;
  /** describe-all: how many freshly-described items want a human look. */
  needs_review?: number;
  /** harvest-keywords: how many items a keyword actually matched. */
  matches?: number;
}

export interface SweepStatus {
  running: boolean;
  /** Null before the sweep registers its counter, and after a relay restart —
   *  it is an in-memory dict, not a row. See `sweepCounts`. */
  progress: SweepProgress | null;
}

const POLL_MS = 2500;
/** Refresh what the page is showing every this many items. The sweep writes
 *  rows for an hour; without this the grid shows the vault as it was on load. */
const REFRESH_EVERY = 10;

export function sweepStatusKey(kind: SweepKind, accountId: string | null): QueryKey {
  return ["vault-sweep", kind, accountId];
}

/** Poll one sweep's status.
 *
 *  Self-arming by default: the interval is a function of the last answer, so it
 *  runs while the sweep runs and stops when it stops — no caller has to know
 *  when to start watching, which is the whole reason a reloaded tab used to see
 *  nothing. `pollWhileIdle` is for a caller that must also watch the gap between
 *  its own start call and the server admitting the sweep exists. */
export function useSweepStatus<T extends SweepStatus = SweepStatus>(
  kind: SweepKind,
  accountId: string | null,
  opts: { enabled?: boolean; pollWhileIdle?: boolean } = {},
): UseQueryResult<T> {
  const { enabled = true, pollWhileIdle = false } = opts;
  return useQuery<T>({
    queryKey: sweepStatusKey(kind, accountId),
    enabled: !!accountId && enabled,
    queryFn: () =>
      relay.get<T>(
        `/admin/vault-ai/${kind}/status?account_id=${encodeURIComponent(accountId!)}`,
      ),
    refetchInterval: (q) => (pollWhileIdle || q.state.data?.running ? POLL_MS : false),
    // Both override an app-wide default (providers.tsx) that is actively wrong
    // for a live counter. staleTime is 3 DAYS there: a mount would be served a
    // cached `running: false` and — since the interval above is a function of
    // exactly that data — would then never poll at all. refetchOnWindowFocus is
    // off there, which would leave a backgrounded tab showing whatever it last
    // saw, on the one screen where you tab away precisely because it is slow.
    staleTime: 0,
    refetchOnWindowFocus: true,
  });
}

export interface VaultSweepOptions {
  kind: SweepKind;
  accountId: string | null;
  /** Refreshed every `REFRESH_EVERY` items while it runs, and once when it
   *  settles — i.e. whatever the sweep is rewriting under the page. */
  liveKeys?: QueryKey[];
  /** Refreshed only when it settles — counts and plans that are meaningless
   *  mid-pass. */
  settleKeys?: QueryKey[];
  /** Fires once, on the running→idle edge, with the final status. */
  onSettled?: (status: SweepStatus) => void;
}

export interface VaultSweep {
  status: SweepStatus | undefined;
  progress: SweepProgress | null;
  running: boolean;
  /** Running, OR a start this tab fired that the server has not confirmed yet.
   *  This is the one to disable a button on. */
  busy: boolean;
  /** POST the start. Extra body fields are merged over `{account_id}`. */
  start: (body?: Record<string, unknown>) => Promise<void>;
}

export function useVaultSweep(opts: VaultSweepOptions): VaultSweep {
  const { kind, accountId } = opts;
  const qc = useQueryClient();
  const query = useSweepStatus(kind, accountId);
  const [starting, setStarting] = useState(false);

  const status = query.data;
  const running = !!status?.running;

  // Latest render's callbacks/keys, so neither effect below has to list a fresh
  // array or closure identity in its dep array — that would re-fire them on
  // every render and turn the settle edge into a loop.
  const latest = useRef(opts);
  latest.current = opts;

  const refresh = useCallback(
    (keys: QueryKey[] | undefined) => {
      for (const key of keys ?? []) qc.invalidateQueries({ queryKey: key });
    },
    [qc],
  );

  const bucket = Math.floor((status?.progress?.done ?? 0) / REFRESH_EVERY);
  useEffect(() => {
    if (!accountId || !running) return;
    refresh(latest.current.liveKeys);
  }, [accountId, running, bucket, refresh]);

  // The running→idle edge, exactly once. Keyed by account so switching the
  // account chip mid-sweep reads as "a different sweep", not as a finish — the
  // previous account's `needs_review` must not pop a modal about this one.
  const seen = useRef<{ acct: string | null; running: boolean }>({
    acct: null,
    running: false,
  });
  useEffect(() => {
    const prev = seen.current;
    seen.current = { acct: accountId, running };
    if (!accountId || prev.acct !== accountId) return; // first sight of this account
    if (running || prev.running === running) return; // only true → false
    refresh(latest.current.liveKeys);
    refresh(latest.current.settleKeys);
    if (status) latest.current.onSettled?.(status);
  }, [accountId, running, status, refresh]);

  const start = useCallback(
    async (body?: Record<string, unknown>) => {
      if (!accountId) return;
      setStarting(true);
      try {
        // Errors are deliberately swallowed. The ordinary one is a 409
        // `*_already_running`, which means the sweep the operator is asking for
        // is already in flight — clearing the label on that is what used to put
        // "Describe all" back over a running sweep. Every other failure is
        // equally uninteresting here, because the refetch below asks the server
        // what is actually true and the UI renders that instead of a guess.
        await relay
          .post(`/admin/vault-ai/${kind}`, { account_id: accountId, ...body }, { accountId })
          .catch(() => {});
        await qc
          .refetchQueries({ queryKey: sweepStatusKey(kind, accountId) })
          .catch(() => {});
      } finally {
        setStarting(false);
      }
    },
    [accountId, kind, qc],
  );

  return {
    status,
    progress: status?.progress ?? null,
    running,
    busy: running || starting,
    start,
  };
}

// ── Label formatters (pure) ────────────────────────────────────────

/** `done`/`total` for a label, or null when there is nothing honest to show.
 *
 *  `fallback` is the DURABLE count, derived from the DB rather than from the
 *  process — describe's plan endpoint gives one for free. The live counter is an
 *  in-memory dict on the relay: it does not exist for the second or two between
 *  registering the sweep and finishing its id select, and it does not survive a
 *  relay restart at all. Reading only the counter is how a sweep that is plainly
 *  running renders as "0/0".
 *
 *  The two are not the same denominator and are not meant to be: the counter
 *  measures THIS PASS, the fallback measures THE VAULT. Both are true, the
 *  fallback only appears when the counter is absent, and it moves in the same
 *  direction — which beats a zero that reads as breakage. */
export function sweepCounts(
  progress: SweepProgress | null,
  fallback?: { done: number; total: number },
): { done: number; total: number } | null {
  if (progress && progress.total > 0) return { done: progress.done, total: progress.total };
  if (fallback && fallback.total > 0) return fallback;
  return null;
}

/** The describe button's live text, plus the tooltip that carries what will not
 *  fit on it. Failures stay off the button (it would wrap) but must not be
 *  invisible: 7 silent audio failures is a shrug, a whole vault failing on a
 *  missing provider key is not, and they look identical without the count. */
export function describeSweepText(
  progress: SweepProgress | null,
  fallback?: { done: number; total: number },
): { label: string; title: string } {
  const counts = sweepCounts(progress, fallback);
  const label = counts
    ? `${counts.done}/${counts.total}${progress?.capped ? " (cap hit)" : ""}`
    : "starting…";
  const title = [
    `Describing ${label} — keeps running if you close this page.`,
    progress?.failed ? `${progress.failed} could not be described.` : "",
    progress?.error ? `First error: ${progress.error}` : "",
  ]
    .filter(Boolean)
    .join("\n");
  return { label, title };
}

export function harvestSweepLabel(progress: SweepProgress | null): string {
  const counts = sweepCounts(progress);
  return counts ? `${counts.done}/${counts.total} · ${progress?.matches ?? 0} hits` : "starting…";
}
