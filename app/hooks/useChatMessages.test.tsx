/**
 * useChatMessages — merge-on-poll (frontend-review-lows fix).
 *
 * Pre-fix the background refetchInterval poll did `return list`, so React
 * Query overwrote the whole cache with just the freshest top page — wiping any
 * load-older pages the user had scrolled up to fetch AND any in-flight
 * optimistic rows. The fix folds the fresh top page INTO the existing cache
 * (mergeTopPage): fresh wins for overlapping ids, older loaded pages stay in
 * front, optimistic / just-arrived rows stay at the tail, and nothing
 * duplicates.
 *
 * These tests drive the real hook through that exact sequence — cold fetch →
 * load-older → optimistic append → poll/refetch — and assert the cache the
 * poll leaves behind. Against the pre-fix `return list` the older pages and
 * the optimistic row would be gone, so the central test fails there.
 */
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from "vitest";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { mergeTopPage, useChatMessages } from "@/hooks/useChatMessages";
import type { OFMessage } from "@/lib/relay";

// The hook talks to the relay + an SSE event bus + a perf logger. Stub them all
// so the test only exercises the cache-merge contract.
vi.mock("@/lib/relay", () => ({ relay: { get: vi.fn() } }));
vi.mock("@/lib/events", () => ({
  eventBus: { isConnected: () => false, onStateChange: () => () => {} },
}));
vi.mock("@/lib/perfLog", () => ({
  perfOpId: () => "op",
  perfLog: () => {},
  perfDelivered: () => {},
  perfError: () => {},
}));

import { relay } from "@/lib/relay";
const relayGet = relay.get as unknown as Mock;

const ACCT = "acct1";
const FAN = 42;
const KEY = ["messages", ACCT, FAN] as const;

function msg(id: number | string, over: Partial<OFMessage> = {}): OFMessage {
  return {
    id,
    text: `m${id}`,
    fromUser: { id: 7 },
    createdAt: "2026-06-04T12:00:00.000Z",
    media: [],
    ...over,
  } as OFMessage;
}

// The relay returns OF's newest-first ("desc") order; the hook reverses it to
// oldest→newest before caching.
function resp(descRows: OFMessage[], hasMore = true) {
  return { list: descRows, hasMore };
}

let client: QueryClient;
function wrapper({ children }: { children: ReactNode }) {
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

const ids = (rows?: OFMessage[]) => (rows ?? []).map((m) => String(m.id));

beforeEach(() => {
  client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  relayGet.mockReset();
});
afterEach(() => { cleanup(); });

describe("useChatMessages merge-on-poll", () => {
  it("cold fetch into an empty cache stores the page as-is (oldest→newest)", async () => {
    relayGet.mockResolvedValue(resp([msg(5), msg(4), msg(3)], true));
    renderHook(() => useChatMessages({ accountId: ACCT, fanId: FAN }), { wrapper });
    await waitFor(() => expect(client.getQueryData<OFMessage[]>(KEY)).toBeDefined());
    expect(ids(client.getQueryData<OFMessage[]>(KEY))).toEqual(["3", "4", "5"]);
  });

  it("a poll merges the fresh top page WITHOUT dropping older pages or optimistic rows", async () => {
    // 1) cold fetch → cache = [3,4,5]
    let top = resp([msg(5), msg(4), msg(3)], true);
    const older = resp([msg(2), msg(1)], false);
    relayGet.mockImplementation((url: string) =>
      Promise.resolve(url.includes("before_id=") ? older : top),
    );
    const { result } = renderHook(
      () => useChatMessages({ accountId: ACCT, fanId: FAN }),
      { wrapper },
    );
    await waitFor(() => expect(ids(client.getQueryData(KEY))).toEqual(["3", "4", "5"]));

    // 2) scroll-up load-older → cache = [1,2,3,4,5]
    await act(async () => { await result.current.loadOlder(); });
    expect(ids(client.getQueryData(KEY))).toEqual(["1", "2", "3", "4", "5"]);

    // 3) optimistic send appends a pending negative-id row at the tail.
    act(() => {
      result.current.appendLocal(msg(-1, { _tempId: -1, _pending: true, text: "sending…" }));
    });
    expect(ids(client.getQueryData(KEY))).toEqual(["1", "2", "3", "4", "5", "-1"]);

    // 4) the background poll returns a fresh top page: id 5 edited, new id 6,
    //    overlapping 3/4. A bare `return list` would clobber the cache down to
    //    just this page — wiping the older pages [1,2] and the optimistic row.
    top = resp([msg(6), msg(5, { text: "EDITED" }), msg(4), msg(3)], true);
    await act(async () => { await result.current.refetch(); });

    const after = client.getQueryData<OFMessage[]>(KEY);
    // older pages kept in front, fresh page in the middle, optimistic at tail.
    expect(ids(after)).toEqual(["1", "2", "3", "4", "5", "6", "-1"]);
    // no duplicate ids survived the merge.
    expect(new Set(ids(after)).size).toBe(ids(after).length);
    // fresh wins for the overlapping id.
    expect(after!.find((m) => String(m.id) === "5")!.text).toBe("EDITED");
    // the optimistic row is still pending (not wiped by the poll).
    expect(after!.find((m) => m._tempId === -1)?._pending).toBe(true);
  });

  it("a poll that exactly overlaps the cache neither duplicates nor reorders", async () => {
    relayGet.mockResolvedValue(resp([msg(5), msg(4), msg(3)], true));
    const { result } = renderHook(
      () => useChatMessages({ accountId: ACCT, fanId: FAN }),
      { wrapper },
    );
    await waitFor(() => expect(ids(client.getQueryData(KEY))).toEqual(["3", "4", "5"]));

    await act(async () => { await result.current.refetch(); });

    // Same ids, same order, no growth from the overlap.
    expect(ids(client.getQueryData(KEY))).toEqual(["3", "4", "5"]);
  });
});

// mergeTopPage classifies a cached row not in the fresh page by its id span.
// The deleted-on-OF drop is the orphan fix: a PPV/message unsent upstream
// vanishes from OF's response but used to linger pinned to the bottom.
describe("mergeTopPage", () => {
  it("drops a cached row inside the page's id span but absent from fresh (deleted on OF)", () => {
    // msg(5) sits between fresh's min(3) and max(7) but OF didn't return it → gone.
    const out = mergeTopPage([msg(3), msg(5), msg(7)], [msg(3), msg(7)]);
    expect(ids(out)).toEqual(["3", "7"]);
  });

  it("the Adrian orphan case: an in-range unsent row at the tail is dropped, not kept", () => {
    // Real shape: a morning opener/PPV (id 100) the fan-thread fetch still spans
    // (min 50 .. max 200) but OF no longer returns → drop.
    const out = mergeTopPage(
      [msg(100), msg(150), msg(200)],
      [msg(50), msg(150), msg(200)],
    );
    expect(ids(out)).toEqual(["50", "150", "200"]);
    expect(ids(out)).not.toContain("100");
  });

  it("keeps scrolled-up history below the page (id < minFreshId) at the front", () => {
    const out = mergeTopPage([msg(1), msg(2)], [msg(5), msg(6)]);
    expect(ids(out)).toEqual(["1", "2", "5", "6"]);
  });

  it("keeps a newer-than-page race row (id > maxFreshId) at the tail", () => {
    const out = mergeTopPage([msg(9)], [msg(5), msg(6)]);
    expect(ids(out)).toEqual(["5", "6", "9"]);
  });

  it("keeps an in-flight optimistic row (id <= 0) at the tail, never drops it", () => {
    const out = mergeTopPage([msg(-1)], [msg(5)]);
    expect(ids(out)).toEqual(["5", "-1"]);
  });

  it("an empty/failed fetch never nukes the cache", () => {
    const out = mergeTopPage([msg(3), msg(5)], []);
    expect(ids(out)).toEqual(["3", "5"]);
  });

  it("combined: keep history, drop the deleted middle, keep the race row", () => {
    const out = mergeTopPage([msg(1), msg(4), msg(9)], [msg(3), msg(5)]);
    expect(ids(out)).toEqual(["1", "3", "5", "9"]);
  });

  it("empty prev → returns fresh unchanged", () => {
    const fresh = [msg(5), msg(6)];
    expect(mergeTopPage([], fresh)).toBe(fresh);
  });
});
