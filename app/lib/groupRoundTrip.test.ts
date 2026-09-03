/**
 * The group lane's id round trip.
 *
 * `parseSlotsParam` used to read the id back with `Number(...)`, so every
 * "👥 group" click on a Fansly chat — and the money rail's "open last 8
 * buyers" — landed on a rounded snowflake that names no real fan. The writer
 * (`encodeSlots`, module-private) was always correct: it interpolates the id
 * as text. These drive the public parser with exactly the strings it writes.
 */
import { describe, expect, it } from "vitest";

import { parseSlotsParam } from "@/lib/groupChannel";

describe("parseSlotsParam id fidelity", () => {
  it("preserves a Fansly snowflake exactly", () => {
    const fanId = "951404711209099264";
    const round = parseSlotsParam(`789937824869654528:${fanId}`);
    expect(round).toHaveLength(1);
    expect(String(round[0].fanId)).toBe(fanId);
    // The old behaviour, pinned so it cannot silently return.
    expect(String(Number(fanId))).toBe("951404711209099300");
  });

  it("keeps a numeric OnlyFans id a number, so its keys still match the inbox", () => {
    const round = parseSlotsParam("acc:582113372");
    expect(round[0].fanId).toBe(582113372);
  });

  it("still drops malformed slots, including the zero the old `> 0` guard caught", () => {
    expect(parseSlotsParam("acc:0,acc:-1,acc:abc,:5,garbage")).toEqual([]);
  });

  it("de-duplicates the same fan across id shapes", () => {
    // "7" and 7 are one fan; the slot list must not hold both.
    expect(parseSlotsParam("acc:7,acc:7")).toHaveLength(1);
  });
});
