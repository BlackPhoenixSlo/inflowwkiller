import { describe, it, expect } from "vitest";

import type { OFMessage } from "@/lib/relay";
import { insertByCreatedAt, resolveChatTarget, toUtcIso } from "./useInboxRealtime";

// Minimal row factory — insertByCreatedAt only reads id + createdAt.
const row = (id: number, createdAt: string): OFMessage =>
  ({ id, createdAt }) as unknown as OFMessage;
const ids = (list: OFMessage[]) => list.map((m) => m.id);

// The WORKER→SSE bridge inverts the legacy "skip outbound" rule: worker-emitted
// outbound events now carry the recipient (__fan_id), so they CAN be routed.
// resolveChatTarget is where that decision lives — pin every branch.
describe("resolveChatTarget", () => {
  it("inbound → targets the sender's chat", () => {
    const t = resolveChatTarget({
      __account_id: "100",
      api2_chat_message: { id: 5, fromUser: { id: 777 } },
    });
    expect(t).toMatchObject({ accountId: "100", fanId: 777, isOutbound: false, fromId: 777 });
  });

  it("outbound with __fan_id → targets the recipient, not the sender", () => {
    const t = resolveChatTarget({
      __account_id: "100",
      __fan_id: 888,
      api2_chat_message: { id: 6, fromUser: { id: 100 } },
    });
    expect(t).toMatchObject({ accountId: "100", fanId: 888, isOutbound: true, fromId: 100 });
  });

  it("outbound falls back to toUser.id when __fan_id is absent", () => {
    const t = resolveChatTarget({
      __account_id: "100",
      api2_chat_message: { id: 7, fromUser: { id: 100 }, toUser: { id: 999 } },
    });
    expect(t).toMatchObject({ fanId: 999, isOutbound: true });
  });

  it("outbound with NO recipient is unroutable → null (legacy skip preserved)", () => {
    expect(
      resolveChatTarget({
        __account_id: "100",
        api2_chat_message: { id: 8, fromUser: { id: 100 } },
      }),
    ).toBeNull();
  });

  it("malformed events → null", () => {
    expect(resolveChatTarget({})).toBeNull();
    expect(resolveChatTarget({ __account_id: "100" })).toBeNull();
    expect(resolveChatTarget({ __account_id: "100", api2_chat_message: { id: 9 } })).toBeNull();
  });

  it("non-numeric fromUser id → null", () => {
    expect(
      resolveChatTarget({
        __account_id: "100",
        api2_chat_message: { id: 10, fromUser: { id: "abc" as unknown as number } },
      }),
    ).toBeNull();
  });
});

// The relay's emit_live rows carry a tz-NAIVE UTC wall clock; REST rows carry a
// zone. toUtcIso reconciles them so live bubbles show the right time and sort
// against REST rows consistently.
describe("toUtcIso", () => {
  it("tags a zone-less wall clock as UTC", () => {
    expect(toUtcIso("2026-06-14 09:36:17")).toBe("2026-06-14T09:36:17.000Z");
  });

  it("truncates sub-millisecond precision on a zone-less value", () => {
    expect(toUtcIso("2026-06-14 09:36:17.000000")).toBe("2026-06-14T09:36:17.000Z");
  });

  it("passes through an already-zoned value (Z and +00:00 offset)", () => {
    expect(toUtcIso("2026-06-14T09:36:17Z")).toBe("2026-06-14T09:36:17.000Z");
    expect(toUtcIso("2026-06-14T09:36:17+00:00")).toBe("2026-06-14T09:36:17.000Z");
  });

  it("empty/undefined → a valid current ISO string", () => {
    expect(toUtcIso(undefined)).toMatch(/Z$/);
    expect(toUtcIso("")).toMatch(/Z$/);
  });

  it("unparseable garbage → returned unchanged (never throws)", () => {
    expect(toUtcIso("not a date")).toBe("not a date");
  });
});

// The append-vs-insert fix: a live message must land where its created_at
// belongs, NOT pinned to the bottom when it arrives after newer rows.
describe("insertByCreatedAt", () => {
  it("a genuinely-newest message appends at the tail (the common path)", () => {
    const prev = [row(1, "2026-06-14T08:00:00Z"), row(2, "2026-06-14T09:00:00Z")];
    expect(ids(insertByCreatedAt(prev, row(3, "2026-06-14T10:00:00Z")))).toEqual([1, 2, 3]);
  });

  it("the bug case: an OLDER live row drops into the middle, not the bottom", () => {
    // Cache already holds the evening rows; a re-run funnel re-emits a morning
    // PPV. Old code would have appended it last; it must slot before the evening.
    const prev = [
      row(10, "2026-06-14T17:23:00Z"),
      row(11, "2026-06-14T19:23:00Z"),
      row(12, "2026-06-14T20:04:00Z"),
    ];
    const out = insertByCreatedAt(prev, row(9, "2026-06-14T09:36:00Z"));
    expect(ids(out)).toEqual([9, 10, 11, 12]);
  });

  it("inserts between the right neighbours", () => {
    const prev = [
      row(1, "2026-06-14T08:00:00Z"),
      row(3, "2026-06-14T10:00:00Z"),
    ];
    expect(ids(insertByCreatedAt(prev, row(2, "2026-06-14T09:00:00Z")))).toEqual([1, 2, 3]);
  });

  it("ties keep arrival order (new equal-timestamp row goes after existing)", () => {
    const prev = [row(1, "2026-06-14T09:00:00Z")];
    expect(ids(insertByCreatedAt(prev, row(2, "2026-06-14T09:00:00Z")))).toEqual([1, 2]);
  });

  it("sorts a zone-less live row consistently against zoned REST rows", () => {
    // Same instant expressed both ways: the live (naive) 09:30 belongs BETWEEN
    // the zoned 09:00 and 10:00 — the exact mismatch that caused the misorder.
    const prev = [
      row(1, "2026-06-14T09:00:00Z"),
      row(3, "2026-06-14T10:00:00Z"),
    ];
    expect(ids(insertByCreatedAt(prev, row(2, "2026-06-14 09:30:00")))).toEqual([1, 2, 3]);
  });

  it("empty cache → single row", () => {
    expect(ids(insertByCreatedAt([], row(1, "2026-06-14T09:00:00Z")))).toEqual([1]);
  });
});
