/**
 * Read receipts and bot attribution compare IDS, never float64.
 *
 * Two id comparisons in replyStateOf outlived the first FanId sweep because
 * both are invisible on the current data:
 *
 *   • `seen: lastRead >= lm.id` — `Math.max` forces a receipt id through
 *     float64 (951605852249276416 -> ...400) and then compares a number
 *     against the wire's STRING id. Fansly sends no receipts yet
 *     (lastReadMessageId is null on every live row, verified 2026-09-02),
 *     which is the only reason it never showed a wrong ✓✓.
 *   • `byBot: lastOutbound.messageId === lm.id` — a `Number()`-parsed
 *     attribution id strict-compared against a string. False for two
 *     independent reasons, so `byBot` could never fire on Fansly.
 *
 * These pin both against the real ids.
 */
import { describe, expect, it } from "vitest";

import { lastOutboundOf, replyStateOf } from "@/components/MoneyRail";

const ACCT = "789937824869654528";
const MSG = "951605852249276416";        // real id; Number() rounds it to ...400
const OF_ACCT = "123456789";
const OF_MSG = 5123456789;

const chat = (over: Record<string, unknown> = {}) => ({
  lastMessage: { id: MSG, fromUser: { id: ACCT }, text: "hi", createdAt: "2026-09-02T12:00:00Z" },
  ...over,
});

describe("lastOutboundOf", () => {
  it("keeps a Fansly snowflake exact instead of rounding it", () => {
    const got = lastOutboundOf({ by_msg_id: { [MSG]: { automation_kind: "welcome_chatter_for_info" } } } as never);
    expect(String(got?.messageId)).toBe(MSG);          // not "951605852249276400"
    expect(got?.automationKind).toBe("welcome_chatter_for_info");
  });

  it("picks the highest id without arithmetic on strings", () => {
    const lo = "951605852249276416", hi = "951605852249276417";
    const got = lastOutboundOf({ by_msg_id: { [lo]: {}, [hi]: {} } } as never);
    expect(String(got?.messageId)).toBe(hi);
  });
});

describe("byBot", () => {
  it("fires when the attribution row names THIS message (Fansly)", () => {
    const st = replyStateOf(chat() as never, null, ACCT,
      { messageId: MSG, automationKind: "welcome_chatter_for_info" });
    expect(st?.outbound).toBe(true);
    expect(st?.byBot).toBe(true);
  });

  it("stays false for a different message", () => {
    const st = replyStateOf(chat() as never, null, ACCT,
      { messageId: "951605852249276999", automationKind: "welcome_chatter_for_info" });
    expect(st?.byBot).toBe(false);
  });

  it("stays false when a human sent it", () => {
    const st = replyStateOf(chat() as never, null, ACCT,
      { messageId: MSG, automationKind: null });
    expect(st?.byBot).toBe(false);
  });
});

describe("seen (read receipt ✓✓)", () => {
  it("is false with no receipt — Fansly's current state", () => {
    const st = replyStateOf(chat() as never, { lastReadMessageId: null } as never, ACCT);
    expect(st?.seen).toBe(false);
  });

  it("is true once the receipt reaches the message", () => {
    const st = replyStateOf(chat() as never, { lastReadMessageId: MSG } as never, ACCT);
    expect(st?.seen).toBe(true);
  });

  it("is FALSE when the receipt is one id short — the float64 trap", () => {
    // Number() collapses ...415 and ...416 to the same double, so the old
    // code reported "read" for a message they had not read.
    const st = replyStateOf(chat() as never, { lastReadMessageId: "951605852249276415" } as never, ACCT);
    expect(st?.seen).toBe(false);
  });

  it("still works for numeric OnlyFans ids", () => {
    const ofChat = { lastMessage: { id: OF_MSG, fromUser: { id: OF_ACCT }, text: "hi", createdAt: "2026-09-02T12:00:00Z" } };
    expect(replyStateOf(ofChat as never, { lastReadMessageId: OF_MSG } as never, OF_ACCT)?.seen).toBe(true);
    expect(replyStateOf(ofChat as never, { lastReadMessageId: OF_MSG - 1 } as never, OF_ACCT)?.seen).toBe(false);
  });
});
