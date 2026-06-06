/**
 * mergeSeedIntoMessages — the no-clobber fold behind ChatSurface's
 * DB-as-render-source hydration (T-RENDER / Wave 1.3).
 *
 * ChatSurface copies DB-seed rows ONE-TIME into the real
 * `["messages", accountId, fanId]` cache so history renders instantly
 * through the same query the realtime patcher + send-flow write to. These
 * tests pin the merge that keeps that copy from ever fighting the patcher:
 *   • empty cache  → seed becomes the content (the common cold-open);
 *   • populated cache (OF won the race, or a live message patched in during
 *     the cold window) → existing rows stay authoritative; only missing
 *     seed history is prepended, deduped, preserving oldest→newest order;
 *   • a no-op fold returns the SAME reference so observers aren't notified.
 */
import { describe, expect, it } from "vitest";

import { mergeSeedIntoMessages } from "@/hooks/useChatMessagesLocal";
import type { OFMessage } from "@/lib/relay";

function msg(over: Partial<OFMessage> & { id: number | string }): OFMessage {
  return {
    text: "",
    fromUser: { id: 0 },
    createdAt: "2026-06-04T12:00:00.000Z",
    media: [],
    ...over,
  } as OFMessage;
}

const ids = (rows: OFMessage[]) => rows.map((m) => String(m.id));

describe("mergeSeedIntoMessages", () => {
  it("empty cache: seed becomes the content (cold-open hydration)", () => {
    const seed = [msg({ id: 1 }), msg({ id: 2 }), msg({ id: 3 })];
    expect(mergeSeedIntoMessages(undefined, seed)).toBe(seed);
    expect(mergeSeedIntoMessages([], seed)).toBe(seed);
  });

  it("OF won the race: a superset cache is returned UNCHANGED (same ref → no notify)", () => {
    // OF resolved first and holds richer rows (media/flags). The seed is a
    // subset, so nothing is missing — the patcher's cache must be untouched.
    const ofRows = [msg({ id: 1, mediaCount: 2 }), msg({ id: 2 }), msg({ id: 3 })];
    const seed = [msg({ id: 1 }), msg({ id: 2 })];
    const merged = mergeSeedIntoMessages(ofRows, seed);
    expect(merged).toBe(ofRows); // identity: setQueryData(prev => prev) is a no-op
  });

  it("patcher-first: a live message in the cache is kept; seed history prepends in order", () => {
    // A live inbound landed via useInboxRealtime before the seed hydrated.
    const live = msg({ id: 100, text: "just arrived" });
    const seed = [msg({ id: 1 }), msg({ id: 2 }), msg({ id: 3 })];
    const merged = mergeSeedIntoMessages([live], seed);
    // Older seed history goes in front, the live (newest) row stays last.
    expect(ids(merged)).toEqual(["1", "2", "3", "100"]);
    expect(merged[merged.length - 1]).toBe(live);
  });

  it("dedup: rows already in the cache are not duplicated, and the existing (richer) row wins", () => {
    const existingTwo = msg({ id: 2, mediaCount: 5 }); // richer copy already cached
    const seed = [msg({ id: 1 }), msg({ id: 2, mediaCount: 0 }), msg({ id: 3 })];
    const merged = mergeSeedIntoMessages([existingTwo], seed);
    expect(ids(merged)).toEqual(["1", "3", "2"]);
    // id 2 resolves to the authoritative cached row, not the seed's copy.
    expect(merged.find((m) => String(m.id) === "2")).toBe(existingTwo);
  });

  it("dedup normalizes id type: number cache id matches string seed id", () => {
    const existing = msg({ id: 5 }); // number
    const seed = [msg({ id: "5" }), msg({ id: 6 })]; // string id collides with 5
    const merged = mergeSeedIntoMessages([existing], seed);
    // Only id 6 is genuinely missing; "5"/5 are the same message.
    expect(ids(merged)).toEqual(["6", "5"]);
  });
});
