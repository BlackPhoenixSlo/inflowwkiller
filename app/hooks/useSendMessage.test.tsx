/**
 * useSendMessage — filtered chat-list cache scoping (frontend-review-lows fix).
 *
 * patchChatListAfterSend pops the just-sent chat to the top of the inbox.
 * Pre-fix it did that lift-to-top in EVERY ["chats", …] cache — including
 * FILTERED / folder / search views — reordering rows the operator never
 * touched (and a lastMessage edit can't even change whether a row still
 * belongs in that filter). The fix scopes the reorder to the UNFILTERED key;
 * filtered caches only get an in-place patch of a row ALREADY present, never a
 * reorder and never a fresh prepend.
 *
 * We drive the real send() through a stub hookHandle (isolating the chat-list
 * patch from the optimistic-message machinery) and assert each seeded cache
 * scope. The "filtered stays in place" assertion fails against the pre-fix
 * code, which would have lifted the row to the top of the filtered cache too.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { useSendMessage } from "@/hooks/useSendMessage";
import type { OFChatItem } from "@/lib/relay";

vi.mock("@/lib/relay", () => ({
  relay: { get: vi.fn(), post: vi.fn(() => Promise.resolve({ id: 999, media: [] })) },
  RelayError: class RelayError extends Error {},
}));
// useSendMessage imports useChatMessages (for its return type), which drags in
// the SSE bus + perf logger at module load — stub them so the module resolves.
vi.mock("@/lib/events", () => ({
  eventBus: { isConnected: () => false, onStateChange: () => () => {} },
}));
vi.mock("@/lib/perfLog", () => ({
  perfOpId: () => "op", perfLog: () => {}, perfDelivered: () => {}, perfError: () => {},
}));
vi.mock("@/contexts/EmployeeContext", () => ({
  useEmployee: () => ({ current: null }),
}));

const ACCT = "acct1";
const FAN = 42;
const OTHER = 7;

function chatRow(fanId: number): OFChatItem {
  return {
    withUser: { id: fanId, name: `fan${fanId}` },
    __accountId: ACCT,
    hasUnread: true,
    unreadMessagesCount: 1,
    lastMessage: {
      id: 1,
      text: "old",
      fromUser: { id: fanId },
      createdAt: "2026-06-04T00:00:00.000Z",
    },
  };
}

type Page = { rows: OFChatItem[]; hasMore: boolean };
type Infinite = { pages: Page[]; pageParams: unknown[] };
const infinite = (rows: OFChatItem[]): Infinite => ({
  pages: [{ rows, hasMore: false }],
  pageParams: [undefined],
});

// Chat-list keys: ["chats", scope, accountKey, filter, listId, query, limit].
// isUnfiltered ⇔ filter/listId/query (positions 3/4/5) are all null.
const KEY_UNFILTERED = ["chats", "all", ACCT, null, null, null, 30] as const;
const KEY_FILTERED_PRESENT = ["chats", "all", ACCT, "unread", null, null, 30] as const;
const KEY_FILTERED_ABSENT = ["chats", "all", ACCT, "ppv", null, null, 30] as const;

let client: QueryClient;
function wrapper({ children }: { children: ReactNode }) {
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

// Minimal stub for the useChatMessages handle — send() only needs the
// optimistic-write callbacks to exist; the chat-list patch is what we assert.
type Handle = Parameters<typeof useSendMessage>[0]["hookHandle"];
function stubHandle(): Handle {
  return {
    appendLocal: vi.fn(),
    reconcileLocal: vi.fn(),
    failLocal: vi.fn(),
    dropLocal: vi.fn(),
  } as unknown as Handle;
}

const rowIds = (inf: Infinite | undefined) =>
  (inf?.pages[0].rows ?? []).map((r) => r.withUser.id);

beforeEach(() => {
  // gcTime: Infinity — the chat-list caches we seed have no observer (only the
  // send hook mounts), so a 0 gcTime would garbage-collect them mid-send and
  // getQueryData would read undefined. Keep them alive for the assertions.
  client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } });
});
afterEach(() => { cleanup(); vi.clearAllMocks(); });

describe("useSendMessage chat-list cache scoping", () => {
  it("lifts the row to the top of the UNFILTERED cache but patches filtered caches in place", async () => {
    // Unfiltered + filtered-present both hold the target at index 1 (behind OTHER).
    client.setQueryData(KEY_UNFILTERED, infinite([chatRow(OTHER), chatRow(FAN)]));
    client.setQueryData(KEY_FILTERED_PRESENT, infinite([chatRow(OTHER), chatRow(FAN)]));
    // A filtered cache the fan is NOT in.
    client.setQueryData(KEY_FILTERED_ABSENT, infinite([chatRow(OTHER)]));

    const { result } = renderHook(
      () =>
        useSendMessage({
          accountId: ACCT,
          fanId: FAN,
          fromUserId: 100,
          fromUserName: "you",
          hookHandle: stubHandle(),
        }),
      { wrapper },
    );

    await act(async () => { await result.current.send({ text: "hi there" }); });

    // UNFILTERED: target lifted to the top of page 0 + patched.
    const unfiltered = client.getQueryData<Infinite>(KEY_UNFILTERED)!;
    expect(rowIds(unfiltered)).toEqual([FAN, OTHER]); // reordered to top
    const lifted = unfiltered.pages[0].rows[0];
    expect(lifted.lastMessage?.text).toBe("hi there");
    expect(lifted.hasUnread).toBe(false);
    expect(lifted.unreadMessagesCount).toBe(0);

    // FILTERED (present): patched in place, order UNTOUCHED (no reorder).
    const filtered = client.getQueryData<Infinite>(KEY_FILTERED_PRESENT)!;
    expect(rowIds(filtered)).toEqual([OTHER, FAN]); // NOT lifted to top
    const patched = filtered.pages[0].rows.find((r) => r.withUser.id === FAN)!;
    expect(patched.lastMessage?.text).toBe("hi there");
    expect(patched.hasUnread).toBe(false);

    // FILTERED (absent): completely untouched — no fresh prepend, no edit.
    const absent = client.getQueryData<Infinite>(KEY_FILTERED_ABSENT)!;
    expect(rowIds(absent)).toEqual([OTHER]);
    expect(absent.pages[0].rows[0].lastMessage?.text).toBe("old");
  });

  it("does not bleed the patch into another account's chat-list cache", async () => {
    // Same unfiltered shape, but for a DIFFERENT account — rows match by
    // __accountId, so this account's cache must stay untouched.
    const KEY_OTHER_ACCT = ["chats", "all", "acct2", null, null, null, 30] as const;
    const otherRow: OFChatItem = { ...chatRow(FAN), __accountId: "acct2" };
    client.setQueryData(KEY_UNFILTERED, infinite([chatRow(FAN)]));
    client.setQueryData(KEY_OTHER_ACCT, infinite([otherRow]));

    const { result } = renderHook(
      () =>
        useSendMessage({
          accountId: ACCT,
          fanId: FAN,
          fromUserId: 100,
          fromUserName: "you",
          hookHandle: stubHandle(),
        }),
      { wrapper },
    );

    await act(async () => { await result.current.send({ text: "scoped" }); });

    // Our account's row gets the patch.
    expect(
      client.getQueryData<Infinite>(KEY_UNFILTERED)!.pages[0].rows[0].lastMessage?.text,
    ).toBe("scoped");
    // The other account's row (same fan id, different __accountId) is untouched.
    expect(
      client.getQueryData<Infinite>(KEY_OTHER_ACCT)!.pages[0].rows[0].lastMessage?.text,
    ).toBe("old");
  });
});

describe("useSendMessage scheduled-send failure surfacing", () => {
  // A scheduled send has NO optimistic bubble — the only evidence it exists is
  // the server-side ghost row. So when the relay refuses the enqueue (a Fansly
  // PPV/media schedule 400s), swallowing the rejection left the composer having
  // cleared the textarea and shown "Scheduled ✓" for a message that existed
  // nowhere: the operator's exact "it doesn't work" report. send() must reject
  // so the composer can restore the message and name the reason.
  it.each([
    ["near-term (our executor queue)", 5 * 60_000],
    ["long-delay (OF's native queue)", 60 * 60_000],
  ])("rejects when the %s enqueue fails", async (_label, delayMs) => {
    const { relay } = await import("@/lib/relay");
    (relay.post as ReturnType<typeof vi.fn>).mockRejectedValueOnce(
      new Error("Fansly scheduled sends support text and GIFs for now"),
    );

    const { result } = renderHook(
      () =>
        useSendMessage({
          accountId: ACCT,
          fanId: FAN,
          fromUserId: 100,
          fromUserName: "you",
          hookHandle: stubHandle(),
        }),
      { wrapper },
    );

    await expect(
      act(async () => {
        await result.current.send({
          text: "unlock me",
          price: 20,
          scheduledAt: new Date(Date.now() + delayMs).toISOString(),
        });
      }),
    ).rejects.toThrow(/Fansly scheduled sends/);
  });

  it("still resolves when the enqueue succeeds", async () => {
    const { result } = renderHook(
      () =>
        useSendMessage({
          accountId: ACCT,
          fanId: FAN,
          fromUserId: 100,
          fromUserName: "you",
          hookHandle: stubHandle(),
        }),
      { wrapper },
    );

    await expect(
      act(async () => {
        await result.current.send({
          text: "in a bit",
          scheduledAt: new Date(Date.now() + 5 * 60_000).toISOString(),
        });
      }),
    ).resolves.toBeUndefined();
  });
});
