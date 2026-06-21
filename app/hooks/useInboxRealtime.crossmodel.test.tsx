/**
 * useInboxRealtime — cross-model chat-list leak guard.
 *
 * An inbound DM for one of the principal's models (model A) was being
 * SYNTHESIZED as a brand-new row into ANOTHER of its models' single-account chat
 * list (model B): the realtime handler walked EVERY ["chats", …] cache and
 * injected the row into any unfiltered one, without checking that the cache's
 * SCOPE (queryKey[2] accountKey) actually includes the event's account. So the
 * same fan showed up under a model it doesn't belong to and double-counted
 * unread until the next 60s refetch ("messages come up even if model is not
 * theirs, in swapping").
 *
 * The fix only synthesizes a new row when the event's account is in the cache's
 * scope — the single id (model scope) or the comma-joined included ids (all
 * scope). Bumping an EXISTING row stays unconditional (already correctly scoped).
 *
 * The "model B stays clean" assertion fails against the pre-fix code, which
 * would have injected model A's fan into model B's list.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import type { OFChatItem } from "@/lib/relay";

const MODEL_A = "1000001"; // the model that owns the inbound fan (event account)
const MODEL_B = "1000002"; // a DIFFERENT model under the same principal
const FAN_A = 9001001; // belongs to model A only — the inbound DM's sender
const FAN_B = 111; // an existing fan already in model B's list
const FAN_AB = 222; // an existing fan in model A's list / the all-models cache

// eventBus mock: record handlers so the test can fire api2_chat_message.
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
// Both models belong to THIS principal, so the allowedRef guard (which only
// blocks OTHER owners' models) passes — the scope check is the only thing
// standing between model A's event and model B's cache.
vi.mock("@/hooks/useAccounts", () => ({
  useActiveAccounts: () => [{ id: "1000001" }, { id: "1000002" }],
}));
vi.mock("@/hooks/useServerScheduledSends", () => ({
  scheduledSendsKey: (a: string, f: number) => ["scheduled-sends", a, f],
}));

// eslint-disable-next-line import/first
import { useInboxRealtime } from "@/hooks/useInboxRealtime";

function chatRow(fanId: number, accountId: string): OFChatItem {
  return {
    withUser: { id: fanId, name: `fan${fanId}` },
    __accountId: accountId,
    hasUnread: false,
    unreadMessagesCount: 0,
    lastMessage: {
      id: fanId,
      text: "old",
      fromUser: { id: fanId },
      createdAt: "2026-06-04T00:00:00.000Z",
    },
  } as OFChatItem;
}

type Page = { rows: OFChatItem[]; hasMore: boolean };
type Infinite = { pages: Page[]; pageParams: unknown[] };
const infinite = (rows: OFChatItem[]): Infinite => ({
  pages: [{ rows, hasMore: false }],
  pageParams: [undefined],
});

// ["chats", scope, accountKey, filter, listId, query, limit] — all unfiltered.
const KEY_A = ["chats", "model", MODEL_A, null, null, null, 25] as const;
const KEY_B = ["chats", "model", MODEL_B, null, null, null, 25] as const;
const KEY_ALL = ["chats", "all", `${MODEL_A},${MODEL_B}`, null, null, null, 25] as const;

const ids = (inf: Infinite | undefined) =>
  (inf?.pages.flatMap((p) => p.rows) ?? []).map((r) => r.withUser.id);

let client: QueryClient;
function wrapper({ children }: { children: ReactNode }) {
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

// Fire an SSE event at the handlers the hook registered on mount.
function fire(env: unknown) {
  act(() => {
    handlers.get("api2_chat_message")?.forEach((h) => h(env));
  });
}

beforeEach(() => {
  handlers.clear();
  // gcTime: Infinity — the seeded chat caches have no observer (the hook only
  // setQueryData's them), so a 0 gcTime would collect them mid-test.
  client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } });
});
afterEach(() => { cleanup(); vi.clearAllMocks(); });

describe("useInboxRealtime cross-model injection guard", () => {
  it("an inbound DM for model A lands in A + all, NOT in model B's list", () => {
    client.setQueryData(KEY_A, infinite([chatRow(FAN_AB, MODEL_A)]));
    client.setQueryData(KEY_B, infinite([chatRow(FAN_B, MODEL_B)]));
    client.setQueryData(KEY_ALL, infinite([chatRow(FAN_B, MODEL_B), chatRow(FAN_AB, MODEL_A)]));

    renderHook(() => useInboxRealtime(), { wrapper });

    fire({
      __account_id: MODEL_A,
      api2_chat_message: {
        id: 9001,
        text: "Y wait",
        fromUser: { id: FAN_A, name: "newfan" },
        createdAt: "2026-06-21T17:22:00.000Z",
      },
    });

    // THE LEAK: model B's single-account list must be untouched — FAN_A is A's.
    expect(ids(client.getQueryData<Infinite>(KEY_B))).toEqual([FAN_B]);

    // model A's own list gets the new row, lifted to the top with unread bumped.
    const a = client.getQueryData<Infinite>(KEY_A)!;
    expect(ids(a)[0]).toBe(FAN_A);
    expect(a.pages[0].rows[0].unreadMessagesCount).toBe(1);
    expect(a.pages[0].rows[0].__accountId).toBe(MODEL_A);

    // the all-models cache (scope includes model A) also gets it, at the top.
    expect(ids(client.getQueryData<Infinite>(KEY_ALL))[0]).toBe(FAN_A);
  });

  it("bumps an EXISTING row even in a cache whose scope wouldn't re-create it", () => {
    // FAN_AB already present in the all cache → a fresh inbound bumps it to the
    // top (the existing-row path is unconditional, not gated by scope).
    client.setQueryData(KEY_ALL, infinite([chatRow(FAN_B, MODEL_B), chatRow(FAN_AB, MODEL_A)]));
    renderHook(() => useInboxRealtime(), { wrapper });

    fire({
      __account_id: MODEL_A,
      api2_chat_message: {
        id: 9100,
        text: "back again",
        fromUser: { id: FAN_AB, name: "fan222" },
        createdAt: "2026-06-21T18:00:00.000Z",
      },
    });

    const all = client.getQueryData<Infinite>(KEY_ALL)!;
    expect(ids(all)).toEqual([FAN_AB, FAN_B]); // bumped to top
    expect(all.pages[0].rows[0].lastMessage?.text).toBe("back again");
  });
});
