/**
 * lib/format — `fmtDuration`, the one that used to be three.
 *
 * VaultPicker's `fmtDur`, VaultTile's `fmtDuration` and a fourth copy written
 * for the composer's overlong-video warning were byte-identical apart from
 * their empty case (`""` vs `null`). Collapsing them onto `""` is only safe
 * because every consumer treats the empty value as falsy — VaultTile's
 * `{dur || "▶"}` is the one that would have regressed silently under `??`, so
 * the empty-string contract is pinned here rather than left implied.
 */
import { describe, expect, it } from "vitest";

import { fmtAgo, fmtDuration, fmtEvery } from "./format";

describe("fmtDuration", () => {
  it("renders m:ss", () => {
    expect(fmtDuration(599)).toBe("9:59");
    expect(fmtDuration(300)).toBe("5:00");
    expect(fmtDuration(25)).toBe("0:25");
    expect(fmtDuration(3600)).toBe("60:00");
  });

  it("floors fractional seconds rather than rounding into the next minute", () => {
    expect(fmtDuration(59.9)).toBe("0:59");
  });

  it("returns a FALSY empty string for absent/zero, so callers can fall back", () => {
    // `{dur || "▶"}` in VaultTile depends on this exactly.
    expect(fmtDuration(0)).toBe("");
    expect(fmtDuration(-5)).toBe("");
    expect(fmtDuration(null)).toBe("");
    expect(fmtDuration(undefined)).toBe("");
  });
});

/**
 * `fmtEvery` / `fmtAgo` — the automation-rule pair, promoted out of
 * AutomationsPanel (which owned both) and ReadyMadePanel (a byte-identical
 * `timeAgo`) when a third surface needed them.
 *
 * `fmtEvery`'s "—" is the load-bearing case: a rule on a `daily_at` trigger has
 * NO interval, and any caller that substitutes a default there prints a cadence
 * the rule does not run on.
 */
describe("fmtEvery", () => {
  it("picks the largest clean unit", () => {
    expect(fmtEvery(60)).toBe("every 1 min");
    expect(fmtEvery(300)).toBe("every 5 min");
    expect(fmtEvery(3600)).toBe("every 1 h");
    expect(fmtEvery(7200)).toBe("every 2 h");
    expect(fmtEvery(90)).toBe("every 90 s");
  });

  it("has no interval to name for a daily-clock rule", () => {
    expect(fmtEvery(null)).toBe("—");
    expect(fmtEvery(undefined)).toBe("—");
    expect(fmtEvery(0)).toBe("—");
  });
});

describe("fmtAgo", () => {
  const at = (secsAgo: number) => new Date(Date.now() - secsAgo * 1000).toISOString();

  it("coarsens as it gets older", () => {
    expect(fmtAgo(at(12))).toBe("12s ago");
    expect(fmtAgo(at(5 * 60))).toBe("5 min ago");
    expect(fmtAgo(at(3 * 3600))).toBe("3 h ago");
    expect(fmtAgo(at(2 * 86400))).toBe("2 d ago");
  });

  it("returns null (render nothing) for absent or unparseable stamps", () => {
    expect(fmtAgo(null)).toBeNull();
    expect(fmtAgo(undefined)).toBeNull();
    expect(fmtAgo("not a date")).toBeNull();
  });

  it("reads a future stamp as 'soon' rather than negative time", () => {
    expect(fmtAgo(at(-120))).toBe("soon");
  });
});
