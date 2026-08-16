/**
 * lib/sendFailure — what a bounced send tells us, and what we can see coming.
 *
 * The parser cases are the REAL bodies from the 2026-08-16 incident, copied out
 * of the relay log rather than invented, because the whole point of the module
 * is that OF's precise field survives three layers of wrapping: OF's json →
 * `_proxy`'s `detail.upstream_body` string → RelayError's `body`. A
 * hand-written fixture would only prove the last hop.
 */
import { describe, expect, it } from "vitest";

import { MESSAGE_VIDEO_LIMIT_S, isOverlongForMessage, parseSendFailure } from "./sendFailure";

/** Wrap an OF error body the way server.py `_proxy` hands it to the browser. */
function relayBody(upstream: unknown) {
  return { detail: { upstream_status: 400, upstream_body: JSON.stringify(upstream) } };
}

const OF_400 = {
  error: {
    code: 0,
    message: "Something wrong with attached media, please try to upload it again",
    payload: { removeFromInputMediaIds: [3468787295] },
  },
  errors: {
    mediaFiles: ["Something wrong with attached media, please try to upload it again"],
  },
};

describe("parseSendFailure", () => {
  it("reads the reason AND the refused id from one wrapped 400", () => {
    expect(parseSendFailure(relayBody(OF_400))).toEqual({
      reason: "Something wrong with attached media, please try to upload it again",
      refusedMediaIds: [3468787295],
    });
  });

  it("keeps every id when OF names more than one", () => {
    // The 21:56 send: a 4-media bundle, two of them refused.
    const two = {
      ...OF_400,
      error: { ...OF_400.error, payload: { removeFromInputMediaIds: [3468787295, 3667292993] } },
    };
    expect(parseSendFailure(relayBody(two)).refusedMediaIds).toEqual([3468787295, 3667292993]);
  });

  it("still surfaces OF's sentence when it names no ids", () => {
    const bare = { error: { code: 0, message: "Cannot send message to this user" } };
    expect(parseSendFailure(relayBody(bare))).toEqual({
      reason: "Cannot send message to this user",
      refusedMediaIds: undefined,
    });
  });

  it("prefers the relay's OWN refusal, which never reached OF", () => {
    // The re-sale block answers 409 { detail: { error, message } } — no OF
    // round-trip happened, so there is nothing to mark on the bubble.
    expect(parseSendFailure({ detail: { message: "already owned" } })).toEqual({
      reason: "already owned",
    });
  });

  it("degrades to nothing rather than throwing on a truncated / non-json body", () => {
    // _proxy cuts upstream_body at 2000 chars, so a huge OF error arrives
    // unparseable. The caller falls back to "HTTP 400"; no tile markers.
    expect(parseSendFailure({ detail: { upstream_body: '{"error":{"payl' } })).toEqual({});
    expect(parseSendFailure({ detail: { upstream_body: "<html>502</html>" } })).toEqual({});
    expect(parseSendFailure(undefined)).toEqual({});
    expect(parseSendFailure(null)).toEqual({});
  });

  it("drops junk entries instead of putting NaN on the bubble", () => {
    const junk = { error: { payload: { removeFromInputMediaIds: [3468787295, "nope", null, 0] } } };
    expect(parseSendFailure(relayBody(junk)).refusedMediaIds).toEqual([3468787295]);
  });
});

describe("isOverlongForMessage", () => {
  // The five items from that night — OF's verdict on each is the assertion.
  it("flags exactly the videos OF refused", () => {
    expect(isOverlongForMessage({ duration: 599 })).toBe(true);   // refused
    expect(isOverlongForMessage({ duration: 394 })).toBe(true);   // refused
    expect(isOverlongForMessage({ duration: 279 })).toBe(false);  // sent
    expect(isOverlongForMessage({ duration: 93 })).toBe(false);   // sent
    expect(isOverlongForMessage({ duration: 25 })).toBe(false);   // sent
  });

  it("does not flag media with no duration (photos)", () => {
    expect(isOverlongForMessage({})).toBe(false);
    expect(isOverlongForMessage({ duration: null })).toBe(false);
    expect(isOverlongForMessage(undefined)).toBe(false);
  });

  it("treats the limit itself as sendable", () => {
    expect(isOverlongForMessage({ duration: MESSAGE_VIDEO_LIMIT_S })).toBe(false);
    expect(isOverlongForMessage({ duration: MESSAGE_VIDEO_LIMIT_S + 1 })).toBe(true);
  });
});
