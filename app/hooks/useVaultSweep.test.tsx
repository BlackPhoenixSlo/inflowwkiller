/**
 * REGRESSION PROBE — "we are not printing work in progress".
 *
 * A describe sweep over a 500-item vault runs for over an hour. The progress
 * used to live in a `for (i < 400) { sleep 2500; fetch }` loop inside the click
 * handler, which meant it (a) died on reload, (b) never existed in a second tab,
 * and (c) gave up after ~17 minutes and put "Describe all (N)" back over a sweep
 * that was a third done. The operator's only feedback left was to press the
 * button again — which 409s, which the handler swallowed, which cleared the
 * label again.
 *
 * These lock the four behaviours that fixed it, plus the label maths.
 */
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from "vitest";
import type { ReactNode } from "react";
import { cleanup, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

vi.mock("@/lib/relay", () => ({ relay: { get: vi.fn(), post: vi.fn() } }));

import {
  describeSweepText,
  sweepAbortedNote,
  harvestSweepLabel,
  sweepCounts,
  sweepStatusKey,
  useVaultSweep,
  type SweepStatus,
} from "@/hooks/useVaultSweep";
import { relay } from "@/lib/relay";

const relayGet = relay.get as unknown as Mock;
const relayPost = relay.post as unknown as Mock;
const AID = "506355167";

/** What the fake relay currently reports. Mutate, then refetch. */
let server: SweepStatus;

let client: QueryClient;
function wrapper({ children }: { children: ReactNode }) {
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

beforeEach(() => {
  client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } });
  server = { running: false, progress: null };
  relayGet.mockReset();
  relayPost.mockReset();
  relayGet.mockImplementation(async () => structuredClone(server));
  relayPost.mockResolvedValue({ status: "running" });
});
afterEach(() => cleanup());

function mountSweep(accountId: string | null, onSettled?: (s: SweepStatus) => void) {
  return renderHook(
    ({ acct }: { acct: string | null }) =>
      useVaultSweep({ kind: "describe-all", accountId: acct, onSettled }),
    { wrapper, initialProps: { acct: accountId } },
  );
}

describe("a sweep this tab did not start", () => {
  it("shows progress on mount — the reload / second-tab case", async () => {
    server = { running: true, progress: { total: 500, done: 232, capped: false, failed: 7 } };

    const { result } = mountSweep(AID);

    await waitFor(() => expect(result.current.running).toBe(true));
    expect(result.current.busy).toBe(true);
    expect(result.current.progress).toMatchObject({ done: 232, total: 500 });
    // Nothing was started: the status is asked for unconditionally, which is the
    // whole point — the old loop only ran if THIS tab had pressed the button.
    expect(relayPost).not.toHaveBeenCalled();
  });

  it("pressing the button again does not start a second sweep, and keeps the label", async () => {
    server = { running: true, progress: { total: 500, done: 232, capped: false } };
    // The server's 409 guard (`describe_already_running`) surfaces as a reject.
    relayPost.mockRejectedValue(new Error("409 describe_already_running"));

    const { result } = mountSweep(AID);
    await waitFor(() => expect(result.current.running).toBe(true));

    await result.current.start({ force: false });

    // The rejection is swallowed and the sweep is still shown as running — the
    // old handler cleared its label here, which is what made the button read
    // "Describe all" over a live sweep and invited the next press.
    expect(result.current.busy).toBe(true);
    expect(result.current.progress).toMatchObject({ done: 232 });
    expect(relayPost).toHaveBeenCalledTimes(1);
  });
});

describe("the running→idle edge", () => {
  it("fires onSettled exactly once, with the final status", async () => {
    server = { running: true, progress: { total: 500, done: 499, capped: false } };
    const onSettled = vi.fn();

    const { result } = mountSweep(AID, onSettled);
    await waitFor(() => expect(result.current.running).toBe(true));
    expect(onSettled).not.toHaveBeenCalled();

    server = { running: false, progress: { total: 500, done: 500, capped: false, needs_review: 12 } };
    await client.refetchQueries({ queryKey: sweepStatusKey("describe-all", AID) });
    await waitFor(() => expect(onSettled).toHaveBeenCalledTimes(1));
    expect(onSettled.mock.calls[0][0].progress).toMatchObject({ needs_review: 12 });

    // Later polls of the same idle state must not re-fire it.
    await client.refetchQueries({ queryKey: sweepStatusKey("describe-all", AID) });
    await client.refetchQueries({ queryKey: sweepStatusKey("describe-all", AID) });
    expect(onSettled).toHaveBeenCalledTimes(1);
  });

  it("does not fire when the account chip changes mid-sweep", async () => {
    server = { running: true, progress: { total: 500, done: 232, capped: false } };
    const onSettled = vi.fn();

    const { result, rerender } = mountSweep(AID, onSettled);
    await waitFor(() => expect(result.current.running).toBe(true));

    // Switch to an account with no sweep. running goes true→false, but it is a
    // DIFFERENT sweep — firing here would pop the other account's review modal.
    server = { running: false, progress: null };
    rerender({ acct: "ACCOUNT_ID" });
    await waitFor(() => expect(result.current.running).toBe(false));
    expect(onSettled).not.toHaveBeenCalled();
  });
});

describe("label maths", () => {
  it("prefers the live counter", () => {
    expect(sweepCounts({ total: 500, done: 232 }, { done: 190, total: 463 }))
      .toEqual({ done: 232, total: 500 });
  });

  it("falls back to the durable count when the relay has no counter", () => {
    // `progress: null` is a restarted relay (the counter is an in-memory dict)
    // or the second before the sweep finishes selecting its ids.
    expect(sweepCounts(null, { done: 190, total: 463 })).toEqual({ done: 190, total: 463 });
  });

  it("shows starting… rather than a 0/0 that reads as breakage", () => {
    expect(sweepCounts(null, { done: 0, total: 0 })).toBeNull();
    expect(describeSweepText(null).label).toBe("starting…");
    expect(harvestSweepLabel(null)).toBe("starting…");
  });

  it("keeps failures off the button but in the tooltip", () => {
    const { label, title } = describeSweepText({
      total: 500, done: 232, capped: false, failed: 7,
      error: "deepinfra HTTP 400: cannot identify image file",
    });
    expect(label).toBe("232/500");
    expect(title).toContain("7 could not be described.");
    expect(title).toContain("cannot identify image file");
  });

  it("marks a cap hit on the button, where it changes what you do next", () => {
    expect(describeSweepText({ total: 500, done: 232, capped: true }).label)
      .toBe("232/500 (cap hit)");
  });

  it("says a sweep DIED, because otherwise it looks like one that finished", () => {
    // The whole point: `running: false` + `done < total` is the same two facts
    // for both, so the button read "Describe all (N)" either way and the only
    // feedback an operator had was to press it again — which is exactly what
    // happened on prod for an hour on 2026-08-11.
    const note = sweepAbortedNote({
      total: 832, done: 214, aborted: true,
      error: "RuntimeError: OF read blew up mid-collect",
    });
    expect(note?.text).toContain("618 left");
    expect(note?.text).toContain("Press again");
    expect(note?.detail).toContain("OF read blew up");
  });

  it("stays quiet about a pass that FINISHED, however many items failed", () => {
    // The server keeps its progress snapshot forever and only clears `aborted`
    // at the next start. Surfacing anything else would put a permanent amber
    // banner on the page describing a run from three days ago.
    expect(sweepAbortedNote({ total: 500, done: 500, failed: 7, error: "boom" })).toBeNull();
    expect(sweepAbortedNote({ total: 500, done: 500 })).toBeNull();
    expect(sweepAbortedNote(null)).toBeNull();
  });

  it("counts harvest matches", () => {
    expect(harvestSweepLabel({ total: 50, done: 12, matches: 340 })).toBe("12/50 · 340 hits");
  });
});
