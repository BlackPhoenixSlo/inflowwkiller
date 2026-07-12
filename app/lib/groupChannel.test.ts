import { describe, expect, it } from "vitest";

import { GROUP_SLOT_CAP, parseSlotsParam } from "./groupChannel";

describe("parseSlotsParam", () => {
  it("parses acct:fan pairs in order", () => {
    expect(parseSlotsParam("acc1:123,acc2:456")).toEqual([
      { accountId: "acc1", fanId: 123 },
      { accountId: "acc2", fanId: 456 },
    ]);
  });

  it("keeps the newest slot when the same fan appears twice", () => {
    expect(parseSlotsParam("acc1:1,acc1:1,acc1:2")).toEqual([
      { accountId: "acc1", fanId: 1 },
      { accountId: "acc1", fanId: 2 },
    ]);
  });

  it("treats the same fan id on different accounts as distinct", () => {
    expect(parseSlotsParam("acc1:9,acc2:9")).toHaveLength(2);
  });

  it("caps at the group slot cap", () => {
    const raw = Array.from({ length: 20 }, (_, i) => `acc:${i + 1}`).join(",");
    expect(parseSlotsParam(raw)).toHaveLength(GROUP_SLOT_CAP);
  });

  it("drops malformed entries instead of throwing", () => {
    expect(parseSlotsParam("acc1:123,garbage,:5,acc2:0,acc3:-1,acc4:abc,acc5:7")).toEqual([
      { accountId: "acc1", fanId: 123 },
      { accountId: "acc5", fanId: 7 },
    ]);
  });

  it("round-trips an account id containing the separator", () => {
    // encodeSlots percent-encodes the account id, so a colon inside it must
    // survive — the parser splits on the LAST colon and decodes.
    expect(parseSlotsParam(`${encodeURIComponent("a:b")}:42`)).toEqual([
      { accountId: "a:b", fanId: 42 },
    ]);
  });

  it("returns nothing for an empty param", () => {
    expect(parseSlotsParam("")).toEqual([]);
  });
});
