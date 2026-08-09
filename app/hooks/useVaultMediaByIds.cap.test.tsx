/**
 * REGRESSION PROBE — the by-id vault fan-out must not release the whole
 * library at once.
 *
 * `useQueries` starts every query the moment it mounts; React Query has no
 * concurrency limit of its own. PPVLibraryTab passes this hook every media id
 * in the library (PPVLibraryTab.tsx:209), so a reload used to put one request
 * per id on the wire simultaneously — and because Next REWRITES `/api/of/*`
 * server-side (next.config.ts:59), the browser's per-origin socket limit is not
 * what the relay sees.
 *
 * The relay caps this route too, and THAT is the safety boundary — any other
 * tab or script bypasses everything in this file. What is asserted here is the
 * cheaper half: the requests never leave the tab, so no inbound socket, no auth
 * middleware and no DB round-trip is spent on a request that is only going to
 * queue anyway.
 */
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from "vitest";
import type { ReactNode } from "react";
import { cleanup, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

vi.mock("@/lib/relay", () => ({ relay: { get: vi.fn(), post: vi.fn() } }));

import { useVaultMediaByIds } from "@/hooks/useVaultMediaByIds";
import { relay } from "@/lib/relay";

const relayGet = relay.get as unknown as Mock;
const AID = "acct-1";

let client: QueryClient;
function wrapper({ children }: { children: ReactNode }) {
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

// The semaphore in the hook is module state — one budget for the whole tab,
// which is the point of it. That makes it shared across cases here too, so a
// case that leaves requests in flight starves every case after it. Each case
// drains through this.
const pending: Array<() => void> = [];

/** A relay.get that never settles until told, recording concurrent overlap. */
function meteredRelay() {
  const release = pending;
  let live = 0;
  let peak = 0;
  relayGet.mockImplementation((path: string) => {
    live += 1;
    peak = Math.max(peak, live);
    const id = Number(path.split("/").pop());
    return new Promise((resolve) => {
      release.push(() => {
        live -= 1;
        resolve({ id, type: "photo", files: {} });
      });
    });
  });
  return {
    get peak() {
      return peak;
    },
    get started() {
      return release.length;
    },
    /** Settle everything issued so far, then let the microtask queue drain. */
    async drain() {
      while (release.length) {
        release.splice(0).forEach((fn) => fn());
        await Promise.resolve();
        await Promise.resolve();
      }
    },
  };
}

beforeEach(() => {
  client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: Infinity } },
  });
  relayGet.mockReset();
});
afterEach(async () => {
  // Settle anything still parked BEFORE unmounting: the hook's semaphore is
  // module state — one budget for the whole tab, which is the point of it — so
  // a case that walks away mid-flight starves every case after it.
  while (pending.length) {
    pending.splice(0).forEach((fn) => fn());
    await Promise.resolve();
    await Promise.resolve();
  }
  cleanup();
});

describe("useVaultMediaByIds concurrency", () => {
  it("does not release a whole library of ids at once", async () => {
    const meter = meteredRelay();
    const ids = Array.from({ length: 60 }, (_, i) => 5000 + i);

    renderHook(() => useVaultMediaByIds(AID, ids), { wrapper });

    // Let every query mount and fire whatever it is going to fire.
    await waitFor(() => expect(meter.started).toBeGreaterThan(0));
    await Promise.resolve();
    await Promise.resolve();

    expect(meter.peak).toBeLessThanOrEqual(6);
    // …and the cap must throttle rather than serialise: one-at-a-time would
    // make a real library resolve at one round-trip per tile.
    expect(meter.peak).toBeGreaterThan(1);
  });

  it("still resolves every id once the queue drains", async () => {
    const meter = meteredRelay();
    const ids = [7001, 7002, 7003, 7004, 7005, 7006, 7007, 7008, 7009, 7010];

    const { result } = renderHook(() => useVaultMediaByIds(AID, ids), { wrapper });

    await waitFor(() => expect(meter.started).toBeGreaterThan(0));
    await meter.drain();

    await waitFor(() => {
      expect(Object.keys(result.current)).toHaveLength(ids.length);
    });
    expect(relayGet).toHaveBeenCalledTimes(ids.length);
  });

  it("releases the slot when a request rejects, so a 404 cannot wedge the queue", async () => {
    // A deleted slot image 404s and the hook does not retry. If the semaphore
    // leaked a slot on the rejection path, one dead id would permanently shrink
    // the cap and enough of them would stop the pane resolving at all.
    relayGet.mockRejectedValue(new Error("404"));
    const ids = Array.from({ length: 12 }, (_, i) => 8000 + i);

    renderHook(() => useVaultMediaByIds(AID, ids), { wrapper });

    await waitFor(() => {
      expect(relayGet).toHaveBeenCalledTimes(ids.length);
    });
  });
});
