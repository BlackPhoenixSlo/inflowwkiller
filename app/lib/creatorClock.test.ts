import { describe, expect, it } from "vitest";

import {
  TIMEZONES, clockOptions, localTimeAtOffset, utcLabel, zoneOffsetNow,
} from "./creatorClock";

/**
 * The creator clock. These are the functions that decide what an operator picks
 * and therefore what a fan is told the time is — Isabelle spent days telling new
 * subscribers "it's Friday night in US" at 07:38 her time because two clocks were
 * stored and the invisible one won.
 *
 * Assertions are RELATIVE (derived the same way twice, or pinned to a zone with no
 * DST) — a fixture pinned to a wall clock would go red twice a year on its own.
 */
describe("utcLabel", () => {
  it("spells a stored clock one way, sign always explicit", () => {
    expect(utcLabel(-4)).toBe("UTC-4");
    expect(utcLabel(2)).toBe("UTC+2");
    // Zero is "unset" in the data model, but if something prints it, it prints
    // like the others rather than a bare "UTC".
    expect(utcLabel(0)).toBe("UTC+0");
  });
});

describe("zoneOffsetNow", () => {
  it("derives the label FROM the number, so the two cannot disagree", () => {
    expect(zoneOffsetNow("UTC")).toEqual({ minutes: 0, label: "UTC+0" });
    // Kolkata is +5:30 year-round — no DST, so this is safe to pin, and it proves
    // the label carries the half hour instead of truncating it away.
    expect(zoneOffsetNow("Asia/Kolkata")).toEqual({ minutes: 330, label: "UTC+5:30" });
  });

  it("reads a western zone as a whole number of negative hours", () => {
    const ny = zoneOffsetNow("America/New_York");
    expect(ny).not.toBeNull();
    // -240 (EDT) or -300 (EST) depending on the season; either is a whole hour.
    expect([-240, -300]).toContain(ny!.minutes);
    expect(ny!.label).toBe(`UTC${ny!.minutes / 60}`);
  });

  it("returns null for a zone Intl does not know, instead of throwing", () => {
    expect(zoneOffsetNow("Not/AZone")).toBeNull();
  });
});

describe("clockOptions", () => {
  const options = clockOptions();

  it("offers whole-hour offsets only — the stored column cannot hold anything else", () => {
    expect(options.length).toBeGreaterThan(5);
    for (const o of options) expect(Number.isInteger(o.offset)).toBe(true);
    // Kolkata is in the curated place list but is +5:30, so it must NOT appear:
    // offering it would write a clock 30 minutes off and call it correct.
    expect(TIMEZONES).toContain("Asia/Kolkata");
    expect(options.some((o) => o.label.includes("Kolkata"))).toBe(false);
  });

  it("is sorted west-to-east and names each offset by its places", () => {
    const offsets = options.map((o) => o.offset);
    expect([...offsets].sort((a, b) => a - b)).toEqual(offsets);
    expect(new Set(offsets).size).toBe(offsets.length);   // one row per offset
    for (const o of options) expect(o.label.startsWith(`${utcLabel(o.offset)} — `)).toBe(true);
  });

  it("puts New York and Toronto on the SAME row — they are one clock", () => {
    const row = options.find((o) => o.label.includes("New York"));
    expect(row?.label).toContain("Toronto");
  });
});

describe("localTimeAtOffset", () => {
  it("is utcnow + offset, the same arithmetic the senders do", () => {
    const expected = (h: number) =>
      new Date(Date.now() + h * 3600_000).toLocaleString("en-US", {
        timeZone: "UTC", weekday: "long", hour: "numeric", minute: "2-digit",
      });
    for (const h of [-10, -4, 0, 2, 9]) expect(localTimeAtOffset(h)).toBe(expected(h));
  });

  it("moves the WEEKDAY when the offset crosses midnight", () => {
    // 24h apart must name different days whatever the hour happens to be now.
    expect(localTimeAtOffset(0)).not.toBe(localTimeAtOffset(24));
  });
});
