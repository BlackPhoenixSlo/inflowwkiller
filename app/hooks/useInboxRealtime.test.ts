import { describe, it, expect } from "vitest";

import { resolveChatTarget } from "./useInboxRealtime";

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
