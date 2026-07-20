/**
 * MessageList — bubble-side stability (T0 / DA bugs 8 & 12).
 *
 * The "all-left-then-spread" flash came from deriving each bubble's side
 * from the async `/users/me` owner id, which is null on first paint. These
 * tests pin the two replacement signals:
 *   • side-from-direction (bug 12): a DB-seeded row carries an explicit
 *     `_side` off the authoritative `direction` column, so it aligns
 *     correctly even when `ownerUserId` is still null.
 *   • side-from-cache (bug 8): a live OF row (no `_side`) aligns on the
 *     FIRST render when the owner id is supplied synchronously (in the app,
 *     ChatSurface hydrates it from the `of-owner:<acct>` localStorage cache).
 */
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { cleanup } from "@testing-library/react";

import { renderWithProviders } from "@/test-utils";
import { MessageList } from "@/components/chat/MessageList";
import type { OFMessage } from "@/lib/relay";

type SeedMsg = OFMessage & { _side?: "left" | "right" };

function msg(over: Partial<SeedMsg> & { id: number | string }): OFMessage {
  return {
    text: "",
    fromUser: { id: 0 },
    createdAt: "2026-06-04T12:00:00.000Z",
    media: [],
    ...over,
  } as OFMessage;
}

const baseProps = {
  accountId: "acct1",
  isLoading: false,
  isError: false,
  hasOlder: false,
  loadingOlder: false,
  onLoadOlder: () => {},
  onRetry: () => {},
};

/** The inner bubble box carries `data-msg-id` plus the side-specific
 *  background: `bg-accent` (outgoing/right) vs `bg-bg-elev-1` (incoming/left). */
function bubbleBox(container: HTMLElement, id: number | string): HTMLElement {
  const el = container.querySelector(`[data-msg-id="${id}"]`);
  if (!el) throw new Error(`no bubble for msg ${id}`);
  return el as HTMLElement;
}

beforeEach(() => {
  window.localStorage.clear();
});
afterEach(() => {
  cleanup();
});

describe("MessageList bubble alignment", () => {
  it("side-from-direction: a DB-seeded row aligns by `direction` even when ownerUserId is null", () => {
    const messages = [
      msg({ id: 1, text: "we sent this", fromUser: { id: 777 }, _side: "right" }),
      msg({ id: 2, text: "fan sent this", fromUser: { id: 42 }, _side: "left" }),
    ];

    const { container } = renderWithProviders(
      <MessageList {...baseProps} messages={messages} ownerUserId={null} />,
    );

    const out = bubbleBox(container, 1);
    const inc = bubbleBox(container, 2);
    // `_side` drives it — no id comparison, no dependence on ownerUserId.
    expect(out.className).toContain("bg-accent");
    expect(out.className).not.toContain("bg-bg-elev-1");
    expect(inc.className).toContain("bg-bg-elev-1");
    expect(inc.className).not.toContain("bg-accent");
  });

  it("side-from-cache: a live row (no _side) aligns right on the FIRST render when the owner id is synchronous", () => {
    // ChatSurface persists the resolved /users/me id here and re-reads it
    // synchronously on the next mount. Simulate that warm cache.
    window.localStorage.setItem("of-owner:acct1", "555");
    const ownerUserId = Number(window.localStorage.getItem("of-owner:acct1"));

    const messages = [
      msg({ id: 10, text: "live outbound", fromUser: { id: 555 } }), // live OF row, no _side
      msg({ id: 11, text: "live inbound", fromUser: { id: 42 } }),
    ];

    const { container } = renderWithProviders(
      <MessageList {...baseProps} messages={messages} ownerUserId={ownerUserId} />,
    );

    // No waitFor: a synchronous owner id means the very first paint is
    // already correctly sided (the whole point of the cache).
    expect(bubbleBox(container, 10).className).toContain("bg-accent");
    expect(bubbleBox(container, 11).className).toContain("bg-bg-elev-1");
  });

  it("side-from-cache control: without an owner id, a live row defaults left (the old flash state)", () => {
    const messages = [msg({ id: 20, text: "live outbound", fromUser: { id: 555 } })];

    const { container } = renderWithProviders(
      <MessageList {...baseProps} messages={messages} ownerUserId={null} />,
    );

    // This is exactly the case the synchronous cache fixes: with no owner
    // id and no _side there is nothing to align against, so it falls left.
    expect(bubbleBox(container, 20).className).toContain("bg-bg-elev-1");
  });
});

// The PPV lock pill unlocks on EITHER OF's `isOpened` OR our ledger's
// `isPaid`. is_paid lands minutes before OF's isOpened echoes on a fresh
// fetch, so a purchased PPV must not linger "locked" on a cached bubble.
describe("MessageList PPV unlock (isOpened || isPaid)", () => {
  function ppv(over: Partial<SeedMsg>): OFMessage {
    // Inbound so the lock pill + 🔒 tile render (outgoing owns the content).
    return msg({ id: 30, text: "", price: 29.99, _side: "left",
      fromUser: { id: 42 }, ...over });
  }

  it("locked when neither isOpened nor isPaid is set", () => {
    const { container } = renderWithProviders(
      <MessageList {...baseProps} messages={[ppv({ isOpened: false, isPaid: false })]} ownerUserId={555} />,
    );
    expect(container.textContent).toContain("locked");
    expect(container.textContent).not.toContain("unlocked");
  });

  it("unlocks on the ledger's isPaid alone, even when OF still reports isOpened=false", () => {
    const { container } = renderWithProviders(
      <MessageList {...baseProps} messages={[ppv({ isOpened: false, isPaid: true })]} ownerUserId={555} />,
    );
    expect(container.textContent).toContain("unlocked");
  });

  it("unlocks on OF's isOpened alone (the pre-existing path still works)", () => {
    const { container } = renderWithProviders(
      <MessageList {...baseProps} messages={[ppv({ isOpened: true })]} ownerUserId={555} />,
    );
    expect(container.textContent).toContain("unlocked");
  });
});
