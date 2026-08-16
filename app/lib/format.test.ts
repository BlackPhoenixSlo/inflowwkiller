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

import { fmtDuration } from "./format";

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
