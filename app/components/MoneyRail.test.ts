import { beforeEach, describe, expect, it } from "vitest";

import { anchorFor, clampToViewport, lastOutboundOf, replyStateOf } from "./MoneyRail";
import type { OFChatItem } from "@/lib/relay";
import type { AttributionResponse } from "@/hooks/useChatAttribution";

const ACCOUNT = "5551";
const ME = 5551;
const FAN = 999;

/** Attribution for the newest outbound row. `kind` null = a human chatter. */
function attrib(messageId: number, kind: string | null): AttributionResponse {
  return {
    by_msg_id: {
      [String(messageId)]: {
        employee_id: kind ? null : 7,
        display_name: kind ? "Automation" : "jack",
        automation_kind: kind,
        sender_name: null,
        color: null,
      },
    },
  };
}

function chat(over: Partial<OFChatItem>): OFChatItem {
  return { withUser: { id: FAN }, ...over } as OFChatItem;
}

/** Freshly fetched /chats/{id}: real ids, real receipt, but up to 60s old. */
function detailWith(lastMsg: {
  id: number;
  text: string;
  from: number;
  at: string;
}, lastReadMessageId?: number): OFChatItem {
  return chat({
    lastMessage: {
      id: lastMsg.id,
      text: lastMsg.text,
      fromUser: { id: lastMsg.from },
      createdAt: lastMsg.at,
    },
    lastReadMessageId,
  });
}

describe("replyStateOf", () => {
  it("shows ✓✓ seen when the fan's read receipt reaches our last message", () => {
    const st = replyStateOf(
      detailWith({ id: 100, text: "<p>u liked it huh</p>", from: ME, at: "2026-07-12T09:18:00Z" }, 100),
      null,
      ACCOUNT,
    );
    expect(st).toMatchObject({ text: "u liked it huh", outbound: true, seen: true, stale: false });
  });

  it("shows a plain ✓ when the receipt is behind our last message", () => {
    const st = replyStateOf(
      detailWith({ id: 101, text: "ok", from: ME, at: "2026-07-12T09:35:00Z" }, 100),
      null,
      ACCOUNT,
    );
    expect(st).toMatchObject({ outbound: true, seen: false });
  });

  it("marks the fan as owed a reply when they spoke last", () => {
    const st = replyStateOf(
      detailWith({ id: 102, text: "Made me hard", from: FAN, at: "2026-07-12T09:23:00Z" }),
      null,
      ACCOUNT,
    );
    // outbound=false is what paints the orange "awaiting your reply" dot.
    expect(st).toMatchObject({ outbound: false, seen: false, unread: false });
  });

  it("uses the optimistic chat-list row when a chatter has just replied", () => {
    // The regression: sending patches the chat-list row with the new TEXT but
    // reuses the previous lastMessage.id (useSendMessage). Preferring the
    // 60s-stale detail fetch here reverted the row to the fan's old message.
    const detail = detailWith(
      { id: 102, text: "Made me hard", from: FAN, at: "2026-07-12T09:23:00Z" },
      102,
    );
    const optimistic = chat({
      lastMessage: {
        id: 102, // stale id — the send hasn't been reconciled yet
        text: "ok",
        fromUser: { id: ME },
        createdAt: "2026-07-12T09:35:00Z",
      },
      lastReadMessageId: 102,
      unreadMessagesCount: 0,
    });
    const st = replyStateOf(detail, optimistic, ACCOUNT);
    expect(st?.text).toBe("ok");
    expect(st?.outbound).toBe(true);
    // The dot must clear: we spoke last, so nobody owes the fan a reply.
    // And we must NOT claim "seen" off the stale id (lastRead 102 >= id 102)
    // — the fan cannot have read a message sent one second ago.
    expect(st?.seen).toBe(false);
    expect(st?.stale).toBe(true);
  });

  it("prefers an SSE-delivered inbound over a stale detail fetch", () => {
    const detail = detailWith({ id: 200, text: "you around?", from: ME, at: "2026-07-12T10:00:00Z" }, 200);
    const live = chat({
      lastMessage: {
        id: 201,
        text: "yeah just got home",
        fromUser: { id: FAN },
        createdAt: "2026-07-12T10:05:00Z",
      },
      lastReadMessageId: 200,
      unreadMessagesCount: 1,
    });
    expect(replyStateOf(detail, live, ACCOUNT)).toMatchObject({
      text: "yeah just got home",
      outbound: false,
      unread: true,
      stale: true,
    });
  });

  it("keeps the trustworthy detail row when both sources agree on time", () => {
    const at = "2026-07-12T09:18:00Z";
    const detail = detailWith({ id: 100, text: "hey", from: ME, at }, 100);
    const cached = chat({
      lastMessage: { id: 0, text: "hey", fromUser: { id: ME }, createdAt: at },
    });
    expect(replyStateOf(detail, cached, ACCOUNT)).toMatchObject({ seen: true, stale: false });
  });

  it("falls back to media count when the last message has no text", () => {
    const st = replyStateOf(
      chat({ lastMessage: { id: 5, text: "", fromUser: { id: ME }, createdAt: "2026-07-12T09:00:00Z", mediaCount: 3 } }),
      null,
      ACCOUNT,
    );
    expect(st?.text).toBe("(3 media)");
  });

  it("returns null for an empty thread", () => {
    expect(replyStateOf(chat({}), null, ACCOUNT)).toBeNull();
    expect(replyStateOf(null, null, ACCOUNT)).toBeNull();
  });
});

describe("replyStateOf — a bot reply doesn't discharge the thread", () => {
  const aiSent = detailWith(
    { id: 300, text: "<p>mm tell me more baby</p>", from: ME, at: "2026-07-12T09:40:00Z" },
    300,
  );

  it("keeps the dot lit when the last message came from an automation", () => {
    const st = replyStateOf(aiSent, null, ACCOUNT, lastOutboundOf(attrib(300, "welcome_chatter_for_info")));
    expect(st).toMatchObject({ outbound: true, byBot: true, seen: true });
  });

  it("clears the dot when a human chatter sent the last message", () => {
    const st = replyStateOf(aiSent, null, ACCOUNT, lastOutboundOf(attrib(300, null)));
    expect(st?.byBot).toBe(false);
  });

  it("does not blame the bot when the attribution row is for an OLDER message", () => {
    // The chatter just replied (id 301); our DB still only knows the AI's 300.
    // Claiming byBot here would flash an orange dot at someone who just replied.
    const st = replyStateOf(
      detailWith({ id: 301, text: "ok", from: ME, at: "2026-07-12T09:41:00Z" }, 300),
      null,
      ACCOUNT,
      lastOutboundOf(attrib(300, "welcome_chatter_for_info")),
    );
    expect(st?.byBot).toBe(false);
  });

  it("stays silent about the sender while the send is still optimistic", () => {
    const detail = detailWith(
      { id: 300, text: "mm tell me more baby", from: ME, at: "2026-07-12T09:40:00Z" },
      300,
    );
    const optimistic = chat({
      lastMessage: { id: 300, text: "ok", fromUser: { id: ME }, createdAt: "2026-07-12T09:45:00Z" },
      lastReadMessageId: 300,
    });
    const st = replyStateOf(detail, optimistic, ACCOUNT, lastOutboundOf(attrib(300, "welcome_chatter_for_info")));
    // The stale optimistic id collides with the AI's real id — without the
    // `stale` guard this would keep the AI's dot on the chatter's own reply.
    expect(st).toMatchObject({ text: "ok", outbound: true, byBot: false, seen: false, stale: true });
  });

  it("has no opinion when attribution is missing entirely", () => {
    expect(lastOutboundOf(undefined)).toBeNull();
    expect(replyStateOf(aiSent, null, ACCOUNT, null)?.byBot).toBe(false);
  });
});


describe("anchorFor", () => {
  const VH = 800;

  it("pins to the bottom when the panel sits in the lower half", () => {
    // Dropped near the bottom → expanding must grow UP, so the bottom edge is
    // the one that stays put.
    expect(anchorFor(700, 68, VH)).toBe("bottom");
  });

  it("pins to the top when the panel sits in the upper half", () => {
    expect(anchorFor(20, 68, VH)).toBe("top");
  });

  it("decides on the panel's centre, not its top edge", () => {
    // Top edge is above the midpoint (390 < 400) but the centre is below it,
    // so this belongs to the bottom half.
    expect(anchorFor(390, 200, VH)).toBe("bottom");
  });
});

describe("clampToViewport", () => {
  beforeEach(() => {
    Object.defineProperty(window, "innerWidth", { value: 1000, writable: true, configurable: true });
    Object.defineProperty(window, "innerHeight", { value: 800, writable: true, configurable: true });
  });

  it("leaves an in-bounds position alone", () => {
    expect(clampToViewport(300, 200, 420)).toEqual({ x: 300, y: 200 });
  });

  it("pulls the panel back inside when it would hang off the edges", () => {
    // 1000 - 290 = 710 is the furthest left the panel can sit.
    expect(clampToViewport(9999, 9999, 420)).toEqual({ x: 710, y: 720 });
    expect(clampToViewport(-50, -50, 420)).toEqual({ x: 0, y: 0 });
  });

  it("always leaves the header grabbable, even for a tall panel", () => {
    // y is capped at innerHeight - 80 regardless of panel height, so the drag
    // handle can never be scrolled off the bottom where it can't be grabbed.
    expect(clampToViewport(0, 5000, 420).y).toBe(720);
  });

  it("re-anchors a spot saved from a bigger window", () => {
    Object.defineProperty(window, "innerWidth", { value: 500, writable: true, configurable: true });
    Object.defineProperty(window, "innerHeight", { value: 400, writable: true, configurable: true });
    expect(clampToViewport(900, 700, 68)).toEqual({ x: 210, y: 332 });
  });
});

