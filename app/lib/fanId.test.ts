/**
 * Identity compares across the two platforms.
 *
 * OnlyFans sends numeric user ids; the Fansly shim sends STRING snowflakes
 * (`of_message` → `"fromUser": {"id": str(senderId)}`). Every "who spoke last"
 * check used to be `lastMessage.fromUser.id !== Number(accountId)`, which is a
 * cross-type `!==` on Fansly and therefore ALWAYS true: the yellow
 * "read · awaiting your reply" dot and the "Owe reply" chip fired on every
 * Fansly thread we had already answered. These cases pin the fix with the real
 * ids from the live account (789937824869654528 / its fan 555465665494917120).
 */
import { describe, expect, it } from "vitest";

import { compareFanIds, fanIdFromParam, isValidFanId, lastMessageFromFan, sameUserId, type FanId } from "@/lib/fanId";

const FANSLY_ME = "789937824869654528";
const FANSLY_FAN = "555465665494917120";
const OF_ME = 1234567;
const OF_FAN = 582113372;

const chat = (fromId: number | string | null) => ({
  lastMessage: fromId == null ? null : { fromUser: { id: fromId } },
});

describe("sameUserId", () => {
  it("matches across the number/string split the two platforms create", () => {
    expect(sameUserId(FANSLY_ME, FANSLY_ME)).toBe(true);
    expect(sameUserId(OF_ME, String(OF_ME))).toBe(true);
    expect(sameUserId(FANSLY_ME, FANSLY_FAN)).toBe(false);
  });

  it("never claims a match on a missing id", () => {
    expect(sameUserId(null, null)).toBe(false);
    expect(sameUserId(undefined, FANSLY_ME)).toBe(false);
    expect(sameUserId(FANSLY_ME, undefined)).toBe(false);
  });
});

describe("lastMessageFromFan", () => {
  it("is FALSE once we replied on Fansly — the bug that pinned the yellow dot", () => {
    expect(lastMessageFromFan(chat(FANSLY_ME), FANSLY_ME)).toBe(false);
    // The old rule, for the record: a string id can never === Number(accountId).
    expect(chat(FANSLY_ME).lastMessage!.fromUser.id !== Number(FANSLY_ME)).toBe(true);
  });

  it("is TRUE when the fan really did speak last", () => {
    expect(lastMessageFromFan(chat(FANSLY_FAN), FANSLY_ME)).toBe(true);
    expect(lastMessageFromFan(chat(OF_FAN), String(OF_ME))).toBe(true);
  });

  it("keeps working on the OnlyFans side (numeric ids, string accountId)", () => {
    expect(lastMessageFromFan(chat(OF_ME), String(OF_ME))).toBe(false);
  });

  it("says nothing is owed when there is no last message to judge", () => {
    // The local-DB inbox seed (/admin/chats/recent) ships lastMessage without a
    // fromUser — an unknown author must not paint a dot.
    expect(lastMessageFromFan(chat(null), FANSLY_ME)).toBe(false);
    expect(lastMessageFromFan({ lastMessage: { fromUser: {} } }, FANSLY_ME)).toBe(false);
    expect(lastMessageFromFan(null, FANSLY_ME)).toBe(false);
  });
});

/**
 * Snowflake precision — the popout's "wrong fan" bug.
 *
 * A Fansly id is bigger than Number.MAX_SAFE_INTEGER, so the old
 * `Number(param)` in /chat/[accountId]/[fanId] rounded 951404711209099264 to
 * 951404711209099300 and the page requested a fan that does not exist ("↗ pop
 * out" → an empty thread + "Couldn't refresh: observe-only"). These pin the
 * identity-preserving parse with the real id from the screenshot.
 */
describe("fanIdFromParam", () => {
  it("keeps a Fansly snowflake EXACT, as a string", () => {
    const raw = "951404711209099264";
    expect(fanIdFromParam(raw)).toBe(raw);
    // The bug, for the record: float64 cannot hold this id.
    expect(Number(raw)).toBe(951404711209099300);
    expect(String(Number(raw))).not.toBe(raw);
  });

  it("keeps an OnlyFans-sized id a NUMBER so its query keys match the inbox's", () => {
    expect(fanIdFromParam("582113372")).toBe(582113372);
    expect(fanIdFromParam("1234567")).toBe(1234567);
  });

  it("round-trips every id the popout is handed", () => {
    for (const raw of ["951404711209099264", "789937824869654528", "555465665494917120", "582113372", "1"]) {
      expect(String(fanIdFromParam(raw))).toBe(raw);
    }
  });

  it("returns null for a param that is not an id, so the page can say so", () => {
    for (const bad of ["", "  ", "abc", "12.5", "-5", "1e21", null, undefined]) {
      expect(fanIdFromParam(bad as string | null | undefined)).toBeNull();
    }
  });
});

/**
 * Ordering. `b - a` is the obvious comparator and it is NaN on a snowflake —
 * a comparator that calls everything equal, so the sort silently stops
 * sorting. useFanSpend depends on this for a STABLE cache key.
 */
describe("compareFanIds", () => {
  it("orders numerically for same-width ids", () => {
    expect(compareFanIds(100, 200)).toBeLessThan(0);
    expect(compareFanIds(200, 100)).toBeGreaterThan(0);
    expect(compareFanIds(100, 100)).toBe(0);
  });

  it("ranks a longer (bigger) snowflake above a short OnlyFans id", () => {
    expect(compareFanIds(OF_FAN, FANSLY_FAN)).toBeLessThan(0);
    expect(compareFanIds(FANSLY_FAN, OF_FAN)).toBeGreaterThan(0);
  });

  it("distinguishes snowflakes that float64 would collapse together", () => {
    // These two differ only past the 2^53 boundary: as numbers they are EQUAL.
    const a = "951404711209099264";
    const b = "951404711209099265";
    expect(Number(a)).toBe(Number(b));
    expect(compareFanIds(a, b)).toBeLessThan(0);
  });

  it("sorts a mixed list stably and totally", () => {
    const ids = [FANSLY_FAN, OF_FAN, FANSLY_ME, 100, "100"];
    const once = [...ids].sort(compareFanIds);
    expect([...once].sort(compareFanIds)).toEqual(once);
    // A real comparator, not the NaN one: something actually moved.
    expect(once.map(String)).not.toEqual(ids.map(String));
  });

  it("agrees with sameUserId on every id shape the wire actually sends", () => {
    // The total-order requirement: compareFanIds' "equal" must mean the same
    // thing as sameUserId's "same account", or useFanSpend's sorted cache key
    // silently splits for one fan. Holds for the real shapes (a number and its
    // own string form); leading zeros are excluded by precondition — neither
    // platform mints them.
    const pairs: Array<[FanId, FanId]> = [[7, "7"], [OF_FAN, String(OF_FAN)], [FANSLY_FAN, FANSLY_FAN]];
    for (const [a, b] of pairs) {
      expect(sameUserId(a, b)).toBe(true);
      expect(compareFanIds(a, b)).toBe(0);
    }
    expect(compareFanIds("7", "9")).toBeLessThan(0);
  });

  it("does not throw on a missing id", () => {
    expect(compareFanIds(null, undefined)).toBe(0);
    expect(compareFanIds(null, OF_FAN)).toBeLessThan(0);
  });
});

/**
 * Validity. `Number.isFinite` was doing this job and is wrong twice: it
 * rejects a good string snowflake unless truncated first, and it accepts
 * `1e21` and `-5`. Zero matters too — parseSlotsParam carried its own
 * `fanId <= 0` guard, and dropping it let "acc:0" through as a real slot.
 */
describe("isValidFanId", () => {
  it("accepts the shapes both platforms actually send", () => {
    expect(isValidFanId(FANSLY_FAN)).toBe(true);
    expect(isValidFanId(OF_FAN)).toBe(true);
    expect(isValidFanId("951404711209099264")).toBe(true);
  });

  it("rejects zero — an integer, but never a fan", () => {
    for (const z of [0, "0", "000"]) expect(isValidFanId(z)).toBe(false);
    expect(fanIdFromParam("0")).toBeNull();
    expect(fanIdFromParam("000")).toBeNull();
  });

  it("rejects garbage that Number.isFinite would have waved through", () => {
    for (const bad of ["abc", "1e21", "-5", "12.5", "", null, undefined, {}, []]) {
      expect(isValidFanId(bad)).toBe(false);
    }
  });
});
