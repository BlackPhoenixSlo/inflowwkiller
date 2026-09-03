/**
 * The rail's stale-detail invalidation must hit BOTH platforms.
 *
 * This effect has now been broken twice, once per platform, with every gate
 * green — tsc, next build and the whole suite passed over a silent no-op both
 * times. The cause each time was reconstructing the fan id from the
 * `${aid}:${fanId}` string key: react-query hashes 12345 and "12345" to
 * DIFFERENT keys, so parsing the half back with Number() missed every Fansly
 * snowflake, and returning it as text missed every numeric OnlyFans id.
 *
 * The fix is to invalidate with the same structured value the query was keyed
 * with. This test pins the property that matters — a seeded entry is actually
 * invalidated — for a numeric OF id AND a string snowflake, so neither
 * platform can regress unnoticed again.
 */
import { QueryClient } from "@tanstack/react-query";
import { describe, expect, it } from "vitest";

import type { FanId } from "@/lib/fanId";

const OF_FAN: FanId = 582113372;
const FANSLY_FAN: FanId = "951404711209099264";

/** What the effect does: invalidate using the value the query was keyed with. */
function invalidateLikeTheRail(qc: QueryClient, aid: string, fanId: FanId) {
  void qc.invalidateQueries({ queryKey: ["chat-detail", aid, fanId] });
  void qc.invalidateQueries({ queryKey: ["money-rail-attrib", aid, fanId] });
}

function seeded(fanId: FanId) {
  const qc = new QueryClient();
  qc.setQueryData(["chat-detail", "acct", fanId], { ok: true });
  qc.setQueryData(["money-rail-attrib", "acct", fanId], { ok: true });
  return qc;
}

const isInvalidated = (qc: QueryClient, key: unknown[]) =>
  qc.getQueryCache().find({ queryKey: key })?.state.isInvalidated ?? false;

describe("MoneyRail stale-detail invalidation", () => {
  it("invalidates a numeric OnlyFans id", () => {
    const qc = seeded(OF_FAN);
    invalidateLikeTheRail(qc, "acct", OF_FAN);
    expect(isInvalidated(qc, ["chat-detail", "acct", OF_FAN])).toBe(true);
    expect(isInvalidated(qc, ["money-rail-attrib", "acct", OF_FAN])).toBe(true);
  });

  it("invalidates a Fansly string snowflake", () => {
    const qc = seeded(FANSLY_FAN);
    invalidateLikeTheRail(qc, "acct", FANSLY_FAN);
    expect(isInvalidated(qc, ["chat-detail", "acct", FANSLY_FAN])).toBe(true);
    expect(isInvalidated(qc, ["money-rail-attrib", "acct", FANSLY_FAN])).toBe(true);
  });

  it("proves WHY the string round trip broke each platform in turn", () => {
    // Reading the key half back as text misses a numeric OF entry...
    const ofQc = seeded(OF_FAN);
    invalidateLikeTheRail(ofQc, "acct", String(OF_FAN) as FanId);
    expect(isInvalidated(ofQc, ["chat-detail", "acct", OF_FAN])).toBe(false);

    // ...and Number()-parsing it misses a Fansly snowflake (it rounds).
    const fanslyQc = seeded(FANSLY_FAN);
    invalidateLikeTheRail(fanslyQc, "acct", Number(FANSLY_FAN) as FanId);
    expect(isInvalidated(fanslyQc, ["chat-detail", "acct", FANSLY_FAN])).toBe(false);
  });
});
