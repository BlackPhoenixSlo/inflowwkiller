/**
 * useChatList — the periodic refresh is HEAD-PAGE-ONLY.
 *
 * React Query's `refetchInterval` on an infinite query re-walks EVERY loaded
 * page sequentially, and each page is itself the multi-account fan-out gated
 * by the slowest account. A user scrolled to page 3 therefore paid three full
 * fan-outs every 90 s to re-read rows that had not changed — ~27% of all inbox
 * list traffic was that redundant re-read.
 *
 * These tests pin the contract the fix has to keep:
 *   • the 90 s poll re-reads offset 0 and nothing else,
 *   • the Refresh button still re-walks every loaded page (it is the user's
 *     only recovery path after missed SSE events),
 *   • a row promoted into the fresh head page renders ONCE, from the head,
 *   • page COUNT stays the cursor — `fetchNextPage` offsets never drift,
 *   • unified scope re-reads offset 0 per account (not "the first N of the
 *     merged list", which is not a stable per-account cursor),
 *   • an active filter rides along on the poll.
 */
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from "vitest";
import { act, cleanup, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

vi.mock("@/lib/relay", () => ({ relay: { get: vi.fn() } }));
vi.mock("@/lib/perfLog", () => ({
  perfOpId: () => "op-test",
  perfLog: vi.fn(),
  perfDelivered: vi.fn(),
  perfError: vi.fn(),
}));

// Scope + account set are module-level so a test can flip between a single
// model and the unified fan-out without a provider tree.
let mockScope: { kind: "model"; accountId: string } | { kind: "all" } =
  { kind: "model", accountId: "acct-a" };
let mockAccounts: { id: string }[] = [{ id: "acct-a" }];
const EMPTY_EXCLUDED = new Set<string>();

vi.mock("@/contexts/ScopeContext", () => ({
  useScope: () => ({
    scope: mockScope,
    setScope: vi.fn(),
    accountId: mockScope.kind === "model" ? mockScope.accountId : null,
    eventScope: "all",
  }),
}));
vi.mock("@/hooks/useAccounts", () => ({
  useActiveAccounts: () => mockAccounts,
}));
// useInboxRealtime's dependencies — it shares this file's relay/account mocks.
type SseHandler = (env: unknown, rawType?: string) => void;
const sseHandlers = new Map<string, Set<SseHandler>>();
vi.mock("@/lib/events", () => ({
  eventBus: {
    on(type: string, fn: SseHandler) {
      let set = sseHandlers.get(type);
      if (!set) { set = new Set(); sseHandlers.set(type, set); }
      set.add(fn);
      return () => set.delete(fn);
    },
  },
}));
vi.mock("@/hooks/useServerScheduledSends", () => ({
  scheduledSendsKey: (a: string, f: number) => ["scheduled-sends", a, f],
}));
vi.mock("@/hooks/useAllModelsInclude", () => ({
  useAllModelsInclude: () => ({
    excluded: EMPTY_EXCLUDED,
    isIncluded: () => true,
    toggle: vi.fn(),
    includeAll: vi.fn(),
    excludeAll: vi.fn(),
  }),
}));

import { useChatList } from "@/hooks/useChatList";
import { useInboxRealtime } from "@/hooks/useInboxRealtime";
import { relay, type OFChatItem } from "@/lib/relay";
const relayGet = relay.get as unknown as Mock;

const POLL_MS = 90_000;

/** A chat row as OF hands it to us: fan id + a sortable lastMessage. */
function row(id: number, createdAt: string): OFChatItem {
  return {
    withUser: { id },
    lastMessage: { id: id * 100, text: `msg-${id}`, createdAt },
    unreadMessagesCount: 0,
  } as OFChatItem;
}

/** Per-test page server, keyed by (accountId, offset). Reassigned mid-test to
 *  simulate the inbox moving underneath a poll. */
let serve: (accountId: string, offset: number) => { list: OFChatItem[]; hasMore: boolean };

interface ChatCall { offset: number; filter: string | null; accountId?: string; refresh?: boolean }

/** Only the /chats calls — enrichment traffic is noise for these assertions. */
function chatCalls(): ChatCall[] {
  return relayGet.mock.calls
    .filter((c) => String(c[0]).startsWith("/api/of/v2/chats"))
    .map((c) => {
      const qs = new URLSearchParams(String(c[0]).split("?")[1] ?? "");
      return {
        offset: Number(qs.get("offset") ?? 0),
        filter: qs.get("filter"),
        accountId: (c[1] as { accountId?: string } | undefined)?.accountId,
        refresh: (c[3] as { refresh?: boolean } | undefined)?.refresh,
      };
    });
}

/** The default single-model key: ["chats", kind, accountKey, filter, listId, query, limit]. */
const MODEL_KEY = ["chats", "model", "acct-a", null, null, null, 25] as const;

/** Watch whether the chats query ever enters `fetching` — the flag ChatList
 *  drives the Refresh button's spinner + disabled state off. Reads the query
 *  STATE, not the rendered result: the rendered value lags a render behind and
 *  would miss the window entirely. */
function watchFetching() {
  let saw = false;
  const stop = client.getQueryCache().subscribe(() => {
    if (client.getQueryState(MODEL_KEY)?.fetchStatus === "fetching") saw = true;
  });
  return { saw: () => saw, stop };
}

let client: QueryClient;
function wrapper({ children }: { children: ReactNode }) {
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

/** Advance fake timers AND drain the promise jobs they release.
 *  Default 1ms, not 0: React Query's notifyManager re-renders observers from a
 *  `setTimeout(cb, 0)`, and vitest's fake clock does not fire that on a 0ms
 *  advance — a cache write would look invisible when it is merely unflushed. */
async function settle(ms = 1) {
  await act(async () => { await vi.advanceTimersByTimeAsync(ms); });
}

/** RTL's waitFor only understands JEST fake timers, so under vitest's it hangs.
 *  Drain microtasks a bounded number of times instead. */
async function until(label: string, cond: () => boolean, tries = 30) {
  for (let i = 0; i < tries; i++) {
    if (cond()) return;
    await settle();
  }
  throw new Error(`timed out waiting for: ${label}`);
}

beforeEach(() => {
  vi.useFakeTimers();
  sseHandlers.clear();
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  mockScope = { kind: "model", accountId: "acct-a" };
  mockAccounts = [{ id: "acct-a" }];
  serve = () => ({ list: [], hasMore: false });
  relayGet.mockReset();
  relayGet.mockImplementation(async (path: string, ctx?: { accountId?: string }) => {
    // Enrichment pass 1 (local SQLite). Answering for every requested id keeps
    // pass 2 (the live OF /users/list) from firing at all.
    if (path.startsWith("/admin/fans/")) {
      const ids = (new URLSearchParams(path.split("?")[1] ?? "").get("ids") ?? "")
        .split(",").filter(Boolean);
      const fans: Record<string, unknown> = {};
      for (const id of ids) {
        fans[id] = {
          id: Number(id), name: `fan-${id}`, username: `fan${id}`,
          avatar: null, customNickname: null,
        };
      }
      return { fans };
    }
    if (path.startsWith("/api/of/v2/chats")) {
      const qs = new URLSearchParams(path.split("?")[1] ?? "");
      return serve(ctx?.accountId ?? "", Number(qs.get("offset") ?? 0));
    }
    throw new Error(`unexpected relay.get ${path}`);
  });
});

afterEach(() => {
  cleanup();
  client.clear();
  vi.useRealTimers();
});

/** Three loaded pages of a single model's inbox, freshly fetched. */
async function renderThreePages(params: Parameters<typeof useChatList>[0] = {}) {
  serve = (_aid, offset) => {
    if (offset === 0) return { list: [row(10, "2026-08-25T10:00:00Z"), row(9, "2026-08-25T09:00:00Z")], hasMore: true };
    if (offset === 25) return { list: [row(8, "2026-08-25T08:00:00Z"), row(7, "2026-08-25T07:00:00Z")], hasMore: true };
    if (offset === 50) return { list: [row(6, "2026-08-25T06:00:00Z")], hasMore: true };
    return { list: [], hasMore: false };
  };
  const h = renderHook(() => useChatList(params), { wrapper });
  await until("page 0", () => h.result.current.data.length === 2);
  await act(async () => { await h.result.current.loadMore(); });
  await act(async () => { await h.result.current.loadMore(); });
  await until("pages 0-2", () => h.result.current.data.length === 5);
  relayGet.mockClear();
  return h;
}

describe("useChatList periodic refresh", () => {
  it("re-reads ONLY the head page on the 90s poll, however deep the list is", async () => {
    await renderThreePages();
    const poll = watchFetching();

    await settle(POLL_MS);
    poll.stop();

    // Pre-fix this was [0, 25, 50] — three fan-outs to re-read four rows
    // nothing had touched.
    expect(chatCalls()).toEqual([
      expect.objectContaining({ offset: 0, accountId: "acct-a" }),
    ]);
    // ...and it never flipped `isFetching`, so the Refresh button stopped
    // spinning + going disabled every 90s. (Probe proven live below.)
    expect(poll.saw()).toBe(false);
  });

  it("still re-walks EVERY loaded page on a manual refresh", async () => {
    const h = await renderThreePages();

    await act(async () => { await h.result.current.refresh(); });

    // The Refresh button is the only recovery path after a missed SSE event:
    // it must reach every loaded page, and force page 0 past the relay cache.
    expect(chatCalls().map((c) => c.offset)).toEqual([0, 25, 50]);
    expect(chatCalls()[0].refresh).toBe(true);
  });

  it("does not leave a poll interval that also walks the deep pages", async () => {
    const h = await renderThreePages();

    // Two polls back to back — a second interval left running elsewhere would
    // show up here as extra offsets.
    await settle(POLL_MS);
    await settle(POLL_MS);

    expect(chatCalls().map((c) => c.offset)).toEqual([0, 0]);
    expect(h.result.current.data.length).toBe(5);
  });

  it("renders a row promoted into the fresh head page exactly once, from the head", async () => {
    const h = await renderThreePages();

    // Fan 6 (page 2) gets a new message and jumps to the top of page 0.
    serve = (_aid, offset) => {
      if (offset === 0) {
        return {
          list: [row(6, "2026-08-25T11:00:00Z"), row(10, "2026-08-25T10:00:00Z")],
          hasMore: true,
        };
      }
      throw new Error(`poll must not read offset ${offset}`);
    };

    await settle(POLL_MS);
    await until("head page spliced", () => h.result.current.data[0]?.withUser.id === 6);

    const ids = h.result.current.data.map((c) => c.withUser.id);
    expect(ids.filter((id) => id === 6)).toHaveLength(1);
    expect(ids[0]).toBe(6);
    // The head copy wins, so the row carries the NEW preview, not page 2's.
    expect(h.result.current.data[0].lastMessage?.createdAt).toBe("2026-08-25T11:00:00Z");
    // ...and the stale deep copy is left in place, so nothing below it shifts.
    // Leaving it there is deliberate: page COUNT is the `fetchNextPage` cursor,
    // so punching a hole in page 2 would make the next fetch skip a server row.
    expect(ids).toEqual([6, 10, 8, 7]);
  });

  it("recovers a row the fresh head page displaced, on manual refresh", async () => {
    // The documented cost of a head-only poll: fan 9 was the tail of page 0, a
    // promotion pushes it to server offset 2 — an offset the STALE page 1
    // predates — so it drops out of the flattened list until a full re-walk.
    const h = await renderThreePages();
    let promoted = false;
    serve = (_aid, offset) => {
      if (offset === 0) {
        return promoted
          ? { list: [row(6, "2026-08-25T11:00:00Z"), row(10, "2026-08-25T10:00:00Z")], hasMore: true }
          : { list: [row(10, "2026-08-25T10:00:00Z"), row(9, "2026-08-25T09:00:00Z")], hasMore: true };
      }
      if (offset === 25) {
        return promoted
          ? { list: [row(9, "2026-08-25T09:00:00Z"), row(8, "2026-08-25T08:00:00Z")], hasMore: true }
          : { list: [row(8, "2026-08-25T08:00:00Z"), row(7, "2026-08-25T07:00:00Z")], hasMore: true };
      }
      return { list: [row(7, "2026-08-25T07:00:00Z")], hasMore: true };
    };
    promoted = true;

    await settle(POLL_MS);
    await until("head spliced", () => h.result.current.data[0]?.withUser.id === 6);
    expect(h.result.current.data.map((c) => c.withUser.id)).not.toContain(9);

    // Refresh re-walks every page — which is exactly why it stays a full
    // refetch. This is the user's recovery path, not a nice-to-have.
    await act(async () => { await h.result.current.refresh(); });
    await until("fan 9 back", () => h.result.current.data.some((c) => c.withUser.id === 9));
    expect(h.result.current.data.map((c) => c.withUser.id)).toEqual([6, 10, 9, 8, 7]);
  });

  it("keeps the next-page offset aligned after a head-only poll", async () => {
    const h = await renderThreePages();

    serve = (_aid, offset) => {
      if (offset === 0) return { list: [row(11, "2026-08-25T12:00:00Z")], hasMore: true };
      if (offset === 75) return { list: [row(5, "2026-08-25T05:00:00Z")], hasMore: false };
      throw new Error(`unexpected offset ${offset}`);
    };

    await settle(POLL_MS);
    expect(chatCalls().map((c) => c.offset)).toEqual([0]);
    await until("head page spliced", () => h.result.current.data[0]?.withUser.id === 11);
    relayGet.mockClear();
    await act(async () => { await h.result.current.loadMore(); });
    await until("page 3", () => h.result.current.data.some((c) => c.withUser.id === 5));

    // Page COUNT is the cursor (`allPages.length * limit`). The poll must not
    // add or drop a page, or the next fetch skips/repeats a server row.
    expect(chatCalls().map((c) => c.offset)).toEqual([75]);
    expect(h.result.current.data.map((c) => c.withUser.id)).toContain(5);
  });

  it("re-reads offset 0 per account in unified scope", async () => {
    mockScope = { kind: "all" };
    mockAccounts = [{ id: "acct-a" }, { id: "acct-b" }, { id: "acct-c" }];
    serve = (aid, offset) => {
      const base = aid.charCodeAt(5) * 10;
      if (offset === 0) return { list: [row(base + 1, "2026-08-25T10:00:00Z")], hasMore: true };
      return { list: [row(base + 2, "2026-08-25T08:00:00Z")], hasMore: false };
    };
    const h = renderHook(() => useChatList(), { wrapper });
    await until("3 accounts x page 0", () => h.result.current.data.length === 3);
    await act(async () => { await h.result.current.loadMore(); });
    await until("3 accounts x 2 pages", () => h.result.current.data.length === 6);
    relayGet.mockClear();

    await settle(POLL_MS);

    const calls = chatCalls();
    expect(calls.map((c) => c.offset)).toEqual([0, 0, 0]);
    expect(calls.map((c) => c.accountId).sort()).toEqual(["acct-a", "acct-b", "acct-c"]);
  });

  it("carries the active filter on the poll", async () => {
    await renderThreePages({ filter: "unread" });

    await settle(POLL_MS);

    expect(chatCalls()).toEqual([expect.objectContaining({ offset: 0, filter: "unread" })]);
  });

  it("polls a single-page list the same way — one head read, no spinner", async () => {
    serve = () => ({ list: [row(10, "2026-08-25T10:00:00Z")], hasMore: false });
    const h = renderHook(() => useChatList(), { wrapper });
    await until("page 0", () => h.result.current.data.length === 1);
    relayGet.mockClear();
    const poll = watchFetching();

    serve = () => ({ list: [row(11, "2026-08-25T11:00:00Z")], hasMore: false });
    await settle(POLL_MS);
    await until("head spliced", () => h.result.current.data[0]?.withUser.id === 11);
    poll.stop();

    expect(chatCalls().map((c) => c.offset)).toEqual([0]);
    // ChatList drives the Refresh button's spinner + disabled state off
    // `isFetching`. A background poll must never touch it, at ANY page depth —
    // the button used to spin and go dead every 90s.
    expect(poll.saw()).toBe(false);

    // ...and the probe is live, not trivially false: the USER's refresh does
    // flip the very flag the poll left alone.
    const manual = watchFetching();
    await act(async () => { await h.result.current.refresh(); });
    manual.stop();
    expect(manual.saw()).toBe(true);
  });

  it("keeps polling a list whose initial fetch errored", async () => {
    // No pages to splice into: the tick hands over to refetch(), which owns the
    // retry AND the error state — the right thing to show on an empty list.
    serve = () => { throw new Error("relay down"); };
    const h = renderHook(() => useChatList(), { wrapper });
    await until("errored", () => h.result.current.isError);
    relayGet.mockClear();
    serve = () => ({ list: [row(10, "2026-08-25T10:00:00Z")], hasMore: false });

    await settle(POLL_MS);
    await until("recovered", () => h.result.current.data.length === 1);

    expect(chatCalls().map((c) => c.offset)).toEqual([0]);
  });

  it("backs the poll off to 5 minutes while the document is hidden", async () => {
    const hidden = vi.spyOn(document, "hidden", "get").mockReturnValue(true);
    try {
      await renderThreePages();

      await settle(POLL_MS);
      expect(chatCalls()).toEqual([]);

      await settle(5 * 60_000);
      expect(chatCalls().map((c) => c.offset)).toEqual([0]);
    } finally {
      hidden.mockRestore();
    }
  });
});

describe("useChatList head poll x useInboxRealtime", () => {
  /** Fire an OF preview event at the handlers useInboxRealtime registered. */
  function fireInbound(fanId: number, text: string, createdAt: string) {
    act(() => {
      sseHandlers.get("api2_chat_message")?.forEach((h) => h({
        __account_id: "acct-a",
        api2_chat_message: {
          id: fanId * 1000, text,
          fromUser: { id: fanId, name: `fan-${fanId}` },
          createdAt,
        },
      }));
    });
  }

  it("SSE still lifts a DEEP row to the head, and the poll leaves that intact", async () => {
    const h = await renderThreePages();
    renderHook(() => useInboxRealtime(), { wrapper });

    // Fan 6 lives on PAGE 2 — the page the poll no longer re-reads. The SSE
    // handler rewrites ALL pages, so this is what keeps deep rows moving.
    fireInbound(6, "hey", "2026-08-25T11:00:00Z");
    await until("sse bump", () => h.result.current.data[0]?.withUser.id === 6);
    expect(h.result.current.data[0].lastMessage?.text).toBe("hey");

    // Now the poll runs. The server has caught up, so its head page agrees.
    serve = (_aid, offset) => {
      if (offset === 0) {
        return {
          list: [row(6, "2026-08-25T11:00:00Z"), row(10, "2026-08-25T10:00:00Z")],
          hasMore: true,
        };
      }
      throw new Error(`poll must not read offset ${offset}`);
    };
    await settle(POLL_MS);
    await until("head spliced", () => h.result.current.data[0]?.lastMessage?.text !== "hey");

    expect(chatCalls().map((c) => c.offset)).toEqual([0]);
    expect(h.result.current.data[0].withUser.id).toBe(6);

    // Page count — the fetchNextPage cursor — survived both rewrites.
    serve = (_aid, offset) => {
      if (offset === 75) return { list: [row(4, "2026-08-25T04:00:00Z")], hasMore: false };
      throw new Error(`unexpected offset ${offset}`);
    };
    relayGet.mockClear();
    await act(async () => { await h.result.current.loadMore(); });
    expect(chatCalls().map((c) => c.offset)).toEqual([75]);
  });
});
