import { describe, expect, it } from "vitest";

import { runStatsChunks } from "./_bits";

/** The rendered line an operator actually reads. */
function line(stats: Record<string, unknown> | null): string {
  return runStatsChunks(stats).map((c) => c.text).join(" · ");
}

describe("runStatsChunks — dry-run reporting", () => {
  it("leads with what the run WOULD do, not how big the pool was", () => {
    // The production shape that hid a six-day no-op: auto_follow aimed at fans
    // it already followed. The old line read "8 candidates · dry run", which
    // an operator reads as "8 follows ready to go".
    const s = line({
      action: "follow", dry_run: true, candidates: 8, examined: 8,
      would_follow: [], already_following: 8,
      paid_profile_skipped: 0, no_price_skipped: 0, errors: 0, cap: 50,
    });
    expect(s).toContain("0 would notify");
    expect(s).toContain("8 already followed");
  });

  it("flags a zero forecast so it cannot be skimmed past", () => {
    // `no_price_skipped` is the marker that says the gate actually ran. Without
    // it the chunk is withheld on purpose (see _bits.tsx) — so every case that
    // asserts a forecast IS rendered has to carry it.
    const chunks = runStatsChunks({
      would_follow: [], candidates: 8, examined: 8, no_price_skipped: 0,
    });
    expect(chunks.find((c) => c.text === "0 would notify")?.tone).toBe("warn");
  });

  it("does not flag a forecast that will actually act", () => {
    const chunks = runStatsChunks({
      would_ping: [1, 2, 3], candidates: 40, examined: 40, no_price_skipped: 0,
    });
    const w = chunks.find((c) => c.text === "3 would notify");
    expect(w).toBeDefined();
    expect(w?.tone).toBeUndefined();
  });

  it("reads would_ping as well as would_follow", () => {
    expect(line({ would_ping: [7001, 7002], examined: 2, no_price_skipped: 0 }))
      .toContain("2 would notify");
  });

  it("says nothing at all when it cannot prove the gate ran", () => {
    // The case the three above used to assert backwards. An older relay fills
    // `would_follow` with the raw, UN-gated pool, so rendering it would restate
    // the six-day lie with more confidence than the wording it replaced.
    // Silence is the deliberate answer; `money_gate: false` is the other way in.
    const s = line({ would_follow: [1, 2, 3], candidates: 8, examined: 8 });
    expect(s).not.toContain("would notify");
    expect(line({ would_follow: [1, 2, 3], money_gate: false }))
      .toContain("would notify");
  });

  it("says when only part of the pool was checked", () => {
    expect(line({ candidates: 250, examined: 50 }))
      .toContain("50 of 250 candidates checked");
    expect(line({ candidates: 8, examined: 8 })).toContain("8 candidates");
  });

  it("still reports a live run's real counters", () => {
    const s = line({
      action: "ping", dry_run: false, pinged: 12, stranded: 0,
      paid_profile_skipped: 3, no_price_skipped: 2, candidates: 148,
    });
    expect(s).toContain("pinged 12");
    expect(s).toContain("3 paid profiles skipped");
    expect(s).toContain("2 price unreadable, skipped");
  });

  it("is silent on an empty bag", () => {
    expect(runStatsChunks(null)).toEqual([]);
    expect(runStatsChunks({})).toEqual([]);
  });
  it("says nothing about a forecast an older relay did not gate", () => {
    // A relay without the gated preview still fills would_follow with the raw
    // pool. Reporting that as "8 would notify" is the original defect wearing a
    // more confident label, so the chunk is withheld until `examined` proves
    // the gate ran.
    const s = line({ dry_run: true, candidates: 8, would_follow: [1, 2, 3, 4, 5, 6, 7, 8] });
    expect(s).not.toContain("would notify");
    expect(s).toContain("8 candidates");
  });
});

describe("runStatsChunks — a live run that notified nobody", () => {
  it("names the pinned window instead of letting it read as a quiet day", () => {
    // `pool_exhausted` is the server's own bit (auto_follow._run_follow): the run
    // walked its whole pool and notified nobody. It was emitted with no consumer
    // at all, so the operator saw `followed 0 · 15 candidates` — the exact line a
    // healthy quiet day prints.
    const s = line({
      action: "follow", dry_run: false, candidates: 15, followed: 0,
      already_following: 15, pool_exhausted: true, errors: 0, cap: 3,
    });
    expect(s).toContain("whole pool checked, nobody new to notify");
    expect(runStatsChunks({ pool_exhausted: true })[0]?.tone).toBe("warn");
  });

  it("stays quiet on a run that did notify someone", () => {
    const s = line({
      action: "follow", dry_run: false, candidates: 15, followed: 3,
      already_following: 12, pool_exhausted: false, errors: 0, cap: 3,
    });
    expect(s).not.toContain("whole pool checked");
    expect(line({ action: "follow", followed: 3 })).not.toContain("whole pool checked");
  });
});
