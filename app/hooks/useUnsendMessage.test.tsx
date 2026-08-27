/**
 * useUnsendMessage — the unsent bubble must not come back on reload.
 *
 * Clicking unsend dropped the row from ["messages", …] and called OF, but the
 * DB-seed cache (useChatMessagesLocal) kept it —
 * and ChatSurface re-hydrates that seed into the thread on every mount. The
 * bubble reappeared as an EMPTY locked shell, since seed rows carry no media
 * and a PPV's mirror body is blank. (The relay flips `messages.is_unsent` on
 * the same DELETE; this is the in-session half of the same fix.)
 */
import { beforeEach, describe, expect, it, vi, type Mock } from "vitest";
import { act, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { seedKey } from "@/hooks/useChatMessagesLocal";
import { useUnsendMessage } from "@/hooks/useUnsendMessage";
import type { OFMessage } from "@/lib/relay";

vi.mock("@/lib/relay", () => ({
  relay: { delete: vi.fn() },
  RelayError: class RelayError extends Error {},
}));

import { relay } from "@/lib/relay";
const relayDelete = relay.delete as unknown as Mock;

const ACCT = "acct1";
const FAN = 42;
const MID = 10977996348669;
// The seed module owns the key shape; spell it out here and the test would
// keep passing after a rename that broke the real prune.
const SEED_KEY = [...seedKey(ACCT, FAN), 30] as const;

function msg(id: number): OFMessage {
  return {
    id, text: "", fromUser: { id: 7 }, createdAt: "2026-08-27T11:03:04.000Z",
    media: [], price: 200,
  } as unknown as OFMessage;
}

let client: QueryClient;
function wrapper({ children }: { children: ReactNode }) {
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

function seedRows() {
  return { messages: [{ message_id: MID }, { message_id: MID - 1 }], limit: 30, next_before_id: null };
}

beforeEach(() => {
  // gcTime: Infinity — these caches have no observers (we write them with
  // setQueryData), and a 0 gcTime evicts them mid-assertion.
  client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } });
  relayDelete.mockReset();
});

const seedIds = () =>
  (client.getQueryData<{ messages: { message_id: number }[] }>(SEED_KEY)?.messages ?? [])
    .map((r) => r.message_id);

describe("useUnsendMessage", () => {
  it("drops the row from the DB-seed cache too", async () => {
    client.setQueryData(["messages", ACCT, FAN], [msg(MID - 1), msg(MID)]);
    client.setQueryData(SEED_KEY, seedRows());
    relayDelete.mockResolvedValue({ success: true });

    const { result } = renderHook(() => useUnsendMessage(ACCT, FAN), { wrapper });
    await act(async () => { await result.current.unsendOne(msg(MID)); });

    expect(client.getQueryData<OFMessage[]>(["messages", ACCT, FAN])?.map((m) => Number(m.id)))
      .toEqual([MID - 1]);
    // Pre-fix this still held MID — ChatSurface's seed hydration painted it
    // back on the next mount as an empty $200 shell.
    expect(seedIds()).toEqual([MID - 1]);
  });

  it("leaves the seed alone when OF refuses (bubble is still live)", async () => {
    client.setQueryData(["messages", ACCT, FAN], [msg(MID)]);
    client.setQueryData(SEED_KEY, seedRows());
    relayDelete.mockRejectedValue(new Error("older than 24h"));

    const { result } = renderHook(() => useUnsendMessage(ACCT, FAN), { wrapper });
    await act(async () => {
      await expect(result.current.unsendOne(msg(MID))).rejects.toThrow();
    });

    expect(client.getQueryData<OFMessage[]>(["messages", ACCT, FAN])?.map((m) => Number(m.id)))
      .toEqual([MID]);
    expect(seedIds()).toEqual([MID, MID - 1]);
  });
});
