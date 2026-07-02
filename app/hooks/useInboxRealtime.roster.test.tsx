/**
 * useInboxRealtime — optimistic roster-badge path.
 *
 * A live message used to fire `refreshOne` on EVERY send/receive — a `?bust` OF
 * round-trip just to recompute one model's badge. We already have the message
 * AND the fan's prior chat row, so the badge now moves LOCALLY (patchLocal) with
 * zero network. This pins:
 *   • rosterDeltaForEvent — prior state + out-of-order guard + dedupe (pure).
 *   • findFanChatRow — the pre-event row lookup.
 *   • the wired handler — inbound bumps the badge with NO relay call; a repeat to
 *     an already-unread fan is a no-op; an automation outbound clears orange; and
 *     a cold fan (not in the list) falls back to ONE authoritative re-read.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import type { OFChatItem } from "@/lib/relay";
import { rosterCountsKey, useRosterCountActions, type RosterCountsResp } from "@/hooks/useRosterCounts";

const MODEL = "1000001"; // owned model (event account)
const COLD_MODEL = "1000009"; // owned model whose fan isn't in the loaded list
const COLD_MODEL2 = "1000010"; // second cold model (distinct refreshOne cooldown key)
const COLD_MODEL3 = "1000011"; // third cold model (N1 authoritative-write test)
const FAN = 9001001;

// eventBus mock: capture handlers so the test can fire api2_chat_message.
type Handler = (env: unknown, rawType?: string) => void;
const handlers = new Map<string, Set<Handler>>();
vi.mock("@/lib/events", () => ({
  eventBus: {
    on(type: string, fn: Handler) {
      let set = handlers.get(type);
      if (!set) { set = new Set(); handlers.set(type, set); }
      set.add(fn);
      return () => set!.delete(fn);
    },
  },
}));
vi.mock("@/hooks/useAccounts", () => ({
  useActiveAccounts: () => [
    { id: MODEL }, { id: COLD_MODEL }, { id: COLD_MODEL2 }, { id: COLD_MODEL3 },
  ],
}));
vi.mock("@/hooks/useServerScheduledSends", () => ({
  scheduledSendsKey: (a: string, f: number) => ["scheduled-sends", a, f],
}));
// relay is spied so we can assert the optimistic path makes NO network call, and
// that the cold path DOES fall back to one authoritative re-read.
vi.mock("@/lib/relay", () => ({ relay: { get: vi.fn(), post: vi.fn() } }));

// eslint-disable-next-line import/first
import { relay } from "@/lib/relay";
// eslint-disable-next-line import/first
import { useInboxRealtime, findFanChatRow, rosterDeltaForEvent } from "@/hooks/useInboxRealtime";

type Page = { rows: OFChatItem[]; hasMore: boolean };
type Infinite = { pages: Page[]; pageParams: unknown[] };
const infinite = (rows: OFChatItem[]): Infinite => ({ pages: [{ rows, hasMore: false }], pageParams: [undefined] });
const chatsKey = (accountId: string) => ["chats", "model", accountId, null, null, null, 25] as const;

/** A chat-list row. `spokeLast` = who authored the cached last message. */
function chatRow(fanId: number, accountId: string, opts?: { unread?: number; spokeLast?: "fan" | "us"; at?: string }): OFChatItem {
  const spoke = opts?.spokeLast ?? "us";
  return {
    withUser: { id: fanId, name: `fan${fanId}` },
    __accountId: accountId,
    hasUnread: (opts?.unread ?? 0) > 0,
    unreadMessagesCount: opts?.unread ?? 0,
    lastMessage: {
      id: fanId,
      text: "old",
      fromUser: { id: spoke === "fan" ? fanId : Number(accountId) },
      createdAt: opts?.at ?? "2026-06-04T00:00:00.000Z",
    },
  } as OFChatItem;
}

let client: QueryClient;
function wrapper({ children }: { children: ReactNode }) {
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}
function fire(env: unknown) {
  act(() => { handlers.get("api2_chat_message")?.forEach((h) => h(env)); });
}
const roster = (accountId: string) =>
  client.getQueryData<RosterCountsResp>(rosterCountsKey("anon"))?.counts?.[accountId];

beforeEach(() => {
  handlers.clear();
  client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } });
});
afterEach(() => { cleanup(); vi.clearAllMocks(); });

describe("rosterDeltaForEvent (pure)", () => {
  const row = (o: Partial<OFChatItem>): OFChatItem =>
    ({ withUser: { id: FAN, name: "f" }, __accountId: MODEL, ...o }) as OFChatItem;

  it("existing == null → null (caller decides the fallback)", () => {
    expect(rosterDeltaForEvent({ isOutbound: false, existing: null, eventCreatedAt: "2026-06-21T10:00:00Z" })).toBeNull();
  });
  it("inbound to a caught-up visible fan → +1 blue", () => {
    const existing = row({ unreadMessagesCount: 0, lastMessage: { id: 1, fromUser: { id: 999 }, createdAt: "2026-06-21T09:00:00Z" } });
    expect(rosterDeltaForEvent({ isOutbound: false, existing, eventCreatedAt: "2026-06-21T10:00:00Z" })).toEqual({ dUnread: 1, dOwe: 0 });
  });
  it("inbound to an already-unread fan → no change (dedupe)", () => {
    const existing = row({ unreadMessagesCount: 3, lastMessage: { id: 1, fromUser: { id: FAN }, createdAt: "2026-06-21T09:00:00Z" } });
    expect(rosterDeltaForEvent({ isOutbound: false, existing, eventCreatedAt: "2026-06-21T10:00:00Z" })).toEqual({ dUnread: 0, dOwe: 0 });
  });
  it("out-of-order: an event OLDER than the cached last message → null (no double-count)", () => {
    const existing = row({ unreadMessagesCount: 0, lastMessage: { id: 1, fromUser: { id: 999 }, createdAt: "2026-06-21T12:00:00Z" } });
    expect(rosterDeltaForEvent({ isOutbound: false, existing, eventCreatedAt: "2026-06-21T09:00:00Z" })).toBeNull();
  });
  it("outbound (automation echo) to an owe-reply fan → -1 orange", () => {
    const existing = row({ unreadMessagesCount: 0, lastMessage: { id: 1, fromUser: { id: FAN }, createdAt: "2026-06-21T09:00:00Z" } });
    expect(rosterDeltaForEvent({ isOutbound: true, existing, eventCreatedAt: "2026-06-21T10:00:00Z" })).toEqual({ dUnread: 0, dOwe: -1 });
  });
});

describe("findFanChatRow", () => {
  it("returns a matching (account, fan) row; null on miss / wrong account", () => {
    const c = new QueryClient();
    c.setQueryData(chatsKey(MODEL), infinite([chatRow(FAN, MODEL)]));
    expect(findFanChatRow(c, MODEL, FAN)?.withUser.id).toBe(FAN);
    expect(findFanChatRow(c, MODEL, 424242)).toBeNull();
    expect(findFanChatRow(c, "2000002", FAN)).toBeNull();
  });

  it("prefers the FRESHEST matching row across cache variants (D5)", () => {
    const c = new QueryClient();
    const stale = chatRow(FAN, MODEL, { unread: 5, at: "2026-06-01T00:00:00.000Z" });
    const fresh = chatRow(FAN, MODEL, { unread: 0, at: "2026-06-21T00:00:00.000Z" });
    // a stale FOLDER variant registered first, the fresh UNFILTERED one second
    c.setQueryData(["chats", "model", MODEL, "priority", null, null, 25], infinite([stale]));
    c.setQueryData(chatsKey(MODEL), infinite([fresh]));
    expect(findFanChatRow(c, MODEL, FAN)?.lastMessage?.createdAt).toBe("2026-06-21T00:00:00.000Z");
  });
});

describe("useInboxRealtime — wired optimistic badge", () => {
  it("inbound to a caught-up fan bumps blue +1 with ZERO relay calls", () => {
    client.setQueryData(chatsKey(MODEL), infinite([chatRow(FAN, MODEL, { unread: 0, spokeLast: "us" })]));
    client.setQueryData(rosterCountsKey("anon"), { counts: { [MODEL]: { unread: 0, owe_reply: 0 } } });
    renderHook(() => useInboxRealtime(), { wrapper });

    fire({
      __account_id: MODEL,
      api2_chat_message: { id: 5000, text: "hey", fromUser: { id: FAN, name: "f" }, createdAt: "2026-06-21T10:00:00.000Z" },
    });

    expect(roster(MODEL)).toEqual({ unread: 1, owe_reply: 0 });
    expect(relay.get).not.toHaveBeenCalled();
  });

  it("a REPEAT inbound to an already-unread fan is a no-op (no triple-count)", () => {
    client.setQueryData(chatsKey(MODEL), infinite([chatRow(FAN, MODEL, { unread: 2, spokeLast: "fan" })]));
    client.setQueryData(rosterCountsKey("anon"), { counts: { [MODEL]: { unread: 1, owe_reply: 0 } } });
    renderHook(() => useInboxRealtime(), { wrapper });

    fire({
      __account_id: MODEL,
      api2_chat_message: { id: 5001, text: "again", fromUser: { id: FAN, name: "f" }, createdAt: "2026-06-21T10:00:00.000Z" },
    });

    expect(roster(MODEL)).toEqual({ unread: 1, owe_reply: 0 });
    expect(relay.get).not.toHaveBeenCalled();
  });

  it("an automation outbound echo clears the orange owe-reply, no relay call", () => {
    client.setQueryData(chatsKey(MODEL), infinite([chatRow(FAN, MODEL, { unread: 0, spokeLast: "fan" })]));
    client.setQueryData(rosterCountsKey("anon"), { counts: { [MODEL]: { unread: 0, owe_reply: 1 } } });
    renderHook(() => useInboxRealtime(), { wrapper });

    fire({
      __account_id: MODEL,
      __fan_id: FAN,
      api2_chat_message: { id: 5002, text: "auto reply", fromUser: { id: Number(MODEL), name: "us" }, createdAt: "2026-06-21T10:00:00.000Z" },
    });

    expect(roster(MODEL)).toEqual({ unread: 0, owe_reply: 0 });
    expect(relay.get).not.toHaveBeenCalled();
  });

  it("a COLD inbound (fan not in the loaded list) falls back to ONE authoritative re-read", async () => {
    // no chats cache seeded for COLD_MODEL → findFanChatRow returns null. refreshOne
    // is async (awaits cancelQueries before relay.get), so its call lands a microtask
    // after fire() returns — waitFor rather than a synchronous assert.
    renderHook(() => useInboxRealtime(), { wrapper });

    fire({
      __account_id: COLD_MODEL,
      api2_chat_message: { id: 5003, text: "who?", fromUser: { id: 777001, name: "new" }, createdAt: "2026-06-21T10:00:00.000Z" },
    });

    await vi.waitFor(() => expect(relay.get).toHaveBeenCalledTimes(1));
    expect(vi.mocked(relay.get).mock.calls[0][0]).toContain("bust=");
  });

  it("does NOT fabricate a roster baseline before the counts have loaded (D1)", () => {
    // chats row present, but roster-counts intentionally NOT seeded (initial poll
    // still in flight). Patching would pin a fake {0,…}+1 over the real fetch.
    client.setQueryData(chatsKey(MODEL), infinite([chatRow(FAN, MODEL, { unread: 0, spokeLast: "us" })]));
    renderHook(() => useInboxRealtime(), { wrapper });

    fire({
      __account_id: MODEL,
      api2_chat_message: { id: 6000, text: "hi", fromUser: { id: FAN, name: "f" }, createdAt: "2026-06-21T10:00:00.000Z" },
    });

    expect(client.getQueryData(rosterCountsKey("anon"))).toBeUndefined();
  });

  it("a COLD OUTBOUND (automation reply to an off-screen fan) also re-reads (D2)", async () => {
    renderHook(() => useInboxRealtime(), { wrapper });

    fire({
      __account_id: COLD_MODEL2,
      __fan_id: 777002,
      api2_chat_message: { id: 6001, text: "auto", fromUser: { id: Number(COLD_MODEL2), name: "us" }, createdAt: "2026-06-21T10:00:00.000Z" },
    });

    await vi.waitFor(() => expect(relay.get).toHaveBeenCalledTimes(1));
  });

  it("an OUT-OF-ORDER inbound moves neither the badge nor the chat row (D3)", () => {
    const fresh = chatRow(FAN, MODEL, { unread: 0, spokeLast: "us", at: "2026-06-21T12:00:00.000Z" });
    client.setQueryData(chatsKey(MODEL), infinite([fresh]));
    client.setQueryData(rosterCountsKey("anon"), { counts: { [MODEL]: { unread: 0, owe_reply: 0 } } });
    renderHook(() => useInboxRealtime(), { wrapper });

    // event is OLDER (09:00) than the cached lastMessage (12:00) → a replay
    fire({
      __account_id: MODEL,
      api2_chat_message: { id: 6002, text: "late replay", fromUser: { id: FAN, name: "f" }, createdAt: "2026-06-21T09:00:00.000Z" },
    });

    expect(roster(MODEL)).toEqual({ unread: 0, owe_reply: 0 });
    const row = client.getQueryData<Infinite>(chatsKey(MODEL))!.pages[0].rows[0];
    expect(row.unreadMessagesCount).toBe(0); // not bumped
    expect(row.lastMessage?.text).toBe("old"); // preview not clobbered by the old msg
    expect(relay.get).not.toHaveBeenCalled();
  });

  it("a cold re-read writes authoritatively even into an EMPTY roster cache (N1)", async () => {
    // roster-counts NOT seeded → the D1 guard would drop patchLocal, but refreshOne
    // must still land its authoritative OF read.
    vi.mocked(relay.get).mockResolvedValueOnce({ counts: { [COLD_MODEL3]: { unread: 7, owe_reply: 2 } } });
    renderHook(() => useInboxRealtime(), { wrapper });

    fire({
      __account_id: COLD_MODEL3,
      api2_chat_message: { id: 6003, text: "hi", fromUser: { id: 777003, name: "n" }, createdAt: "2026-06-21T10:00:00.000Z" },
    });

    await vi.waitFor(() => expect(roster(COLD_MODEL3)).toEqual({ unread: 7, owe_reply: 2 }));
  });
});

describe("refreshOne force bypasses coalescing then reads fresh (N3)", () => {
  it("a forced refresh waits out an in-flight read and does its OWN fresh read", async () => {
    // R1 (forced) reads pre-retry state; R2 (forced, while R1 is in flight) must
    // NOT ride R1 — it waits it out then reads the fresher post-retry state, which
    // wins. Without the drain, R2 would coalesce onto R1 and the badge would stay
    // stale (owe_reply: 1) until the poll.
    let resolveFirst: ((v: RosterCountsResp) => void) | null = null;
    vi.mocked(relay.get)
      .mockImplementationOnce(() => new Promise<RosterCountsResp>((res) => { resolveFirst = res; }))
      .mockResolvedValueOnce({ counts: { [MODEL]: { unread: 0, owe_reply: 0 } } });
    client.setQueryData(rosterCountsKey("anon"), { counts: { [MODEL]: { unread: 0, owe_reply: 1 } } });

    const { result } = renderHook(() => useRosterCountActions(), { wrapper });

    let p1: Promise<void> | undefined;
    let p2: Promise<void> | undefined;
    await act(async () => {
      p1 = result.current.refreshOne(MODEL, { force: true });
      // flush R1's cancelQueries microtask so its relay.get (call 1) fires + binds resolveFirst
      await Promise.resolve();
      await Promise.resolve();
      p2 = result.current.refreshOne(MODEL, { force: true }); // must block on R1, not coalesce
    });
    await act(async () => {
      resolveFirst!({ counts: { [MODEL]: { unread: 0, owe_reply: 1 } } }); // R1 resolves stale
      await p1;
      await p2;
    });

    expect(relay.get).toHaveBeenCalledTimes(2); // the forced R2 did NOT coalesce onto R1
    expect(roster(MODEL)).toEqual({ unread: 0, owe_reply: 0 }); // R2's fresher read won
  });
});
