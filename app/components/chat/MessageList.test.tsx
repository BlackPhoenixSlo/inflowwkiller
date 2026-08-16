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
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
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

/**
 * A failed refresh must not delete the conversation.
 *
 * A fan who deletes his OnlyFans account 404s on every poll, forever. The
 * error branch used to return a full-pane "Failed to load: upstream 404:
 * {"error":{"code":0,"message":"User not found"}}" — blanking a thread whose
 * cached copy was, at that point, the only one left anywhere.
 */
describe("load errors never hide cached history", () => {
  const err = new Error('upstream 404: {"error":{"code":0,"message":"User not found"}}');

  it("keeps rendering cached messages when the refresh 404s", () => {
    const { container } = renderWithProviders(
      <MessageList
        {...baseProps}
        isError
        error={err}
        messages={[msg({ id: 1, text: "cached line" })]}
        ownerUserId={555}
      />,
    );
    expect(container.textContent).toContain("cached line");
  });

  it("shows the failure as a notice above the thread, not instead of it", () => {
    const { container } = renderWithProviders(
      <MessageList
        {...baseProps}
        isError
        error={err}
        messages={[msg({ id: 1, text: "cached line" })]}
        ownerUserId={555}
      />,
    );
    const notice = container.querySelector('[role="status"]');
    expect(notice).not.toBeNull();
    // Plain-language, and it does NOT leak the raw upstream json at the operator.
    expect(notice!.textContent).toContain("no longer exists");
    expect(container.textContent).not.toContain('{"error"');
    // `sticky`, never `absolute` — an absolute banner would cover message #1.
    expect(notice!.className).toContain("sticky");
  });

  it("still blocks with the error when there is nothing cached to show", () => {
    const { container } = renderWithProviders(
      <MessageList {...baseProps} isError error={err} messages={[]} ownerUserId={555} />,
    );
    expect(container.textContent).toContain("no longer exists");
  });

  it("distinguishes transient failures, because those ARE worth retrying", () => {
    const { container } = renderWithProviders(
      <MessageList
        {...baseProps}
        isError
        error={new Error("upstream timeout: ...")}
        messages={[]}
        ownerUserId={555}
      />,
    );
    expect(container.textContent).toContain("timed out");
    expect(container.textContent).not.toContain("no longer exists");
  });
});

/**
 * A failed media send: OF names the offending attachment in
 * `payload.removeFromInputMediaIds`, `useSendMessage` parks it on the bubble as
 * `_failedMediaIds`, and the bubble has to point AT it. On 2026-08-16 the same
 * item was re-picked into three bundles because the footer only ever repeated
 * OF's sentence, which says "media" without saying which.
 */
describe("MessageList refused-attachment marking", () => {
  // jsdom has no IntersectionObserver and MediaTile's lazy path constructs one
  // on mount — these are the only cases here that render tiles at all.
  class IOStub {
    observe() {}
    unobserve() {}
    disconnect() {}
    takeRecords(): unknown[] {
      return [];
    }
  }
  beforeEach(() => {
    vi.stubGlobal("IntersectionObserver", IOStub);
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  /** A failed send of `count` attachments, with the ids at `refusedIdx`
   *  refused. Parameterised on purpose: the first version of these tests
   *  hard-coded TWO tiles, which is under IMAGE_CAP=3, so `items.slice(0, cap)`
   *  could never truncate and the assertions passed no matter what the render
   *  cap did. The incident that produced the feature was a FOUR-media bundle —
   *  the shape the parser test already encodes and the render test could not
   *  express. */
  const failedSend = (count: number, refusedIdx: number[], over: Partial<SeedMsg> = {}): OFMessage => {
    const media = Array.from({ length: count }, (_, i) => ({
      id: 3000000000 + i,
      type: "video",
      files: { thumb: { url: `https://cdn/${i}.jpg` } },
    }));
    return msg({
      id: -1,
      _tempId: -1,
      _failed: true,
      _failedReason: "Something wrong with attached media, please try to upload it again",
      _failedMediaIds: refusedIdx.map((i) => 3000000000 + i),
      text: "sparing",
      price: 144,
      fromUser: { id: 555 },
      media,
      ...over,
    } as Partial<SeedMsg> & { id: number });
  };

  const marks = (c: HTMLElement) =>
    c.querySelectorAll("div[title^='OF refused this attachment']");

  it("counts the refused attachments beside the tiles", () => {
    const { container } = renderWithProviders(
      <MessageList {...baseProps} messages={[failedSend(2, [1])]} ownerUserId={555} />,
    );
    expect(container.textContent).toContain("remove 1 of 2");
  });

  it("marks the tile OF named and leaves its neighbour alone", () => {
    const { container } = renderWithProviders(
      <MessageList {...baseProps} messages={[failedSend(2, [1])]} ownerUserId={555} />,
    );
    expect(marks(container).length).toBe(1);
    expect(marks(container)[0].textContent).toContain("REMOVE");
  });

  it("marks a refused tile that sits PAST the 3-tile render cap", () => {
    // The incident's own shape. Before the fix the row rendered 3 tiles + a
    // "+1" pill, the footer claimed "remove 1 of 4", and nothing was marked.
    const { container } = renderWithProviders(
      <MessageList {...baseProps} messages={[failedSend(4, [3])]} ownerUserId={555} />,
    );
    expect(marks(container).length).toBe(1);
    expect(container.textContent).toContain("remove 1 of 4");
    // Force-opened, so no tile is hidden behind a pill that carries no warning.
    expect(container.textContent).not.toContain("+1");
  });

  it("marks BOTH when OF names one visible and one hidden tile", () => {
    // One mark beside a count of two is worse than none: it reads as a complete
    // answer, so the operator drops that tile and fails identically on the other.
    const { container } = renderWithProviders(
      <MessageList {...baseProps} messages={[failedSend(4, [1, 3])]} ownerUserId={555} />,
    );
    expect(marks(container).length).toBe(2);
    expect(container.textContent).toContain("remove 2 of 4");
  });

  it("never claims a count it cannot point at", () => {
    // OF naming an id this bubble does not hold (a fresh-claim upload whose
    // vault id only exists server-side). The count is an intersection, so it
    // stays silent rather than printing a number over an unmarked row.
    const stray = failedSend(2, [], { _failedMediaIds: [999999999] });
    const { container } = renderWithProviders(
      <MessageList {...baseProps} messages={[stray]} ownerUserId={555} />,
    );
    expect(marks(container).length).toBe(0);
    expect(container.textContent).not.toContain("remove");
    // OF's own sentence still reaches the operator.
    expect(container.textContent).toContain("Something wrong with attached media");
  });

  it("says nothing extra when the failure is not about media", () => {
    const noIds = failedSend(2, [], { _failedMediaIds: undefined });
    const { container } = renderWithProviders(
      <MessageList {...baseProps} messages={[noIds]} ownerUserId={555} />,
    );
    expect(container.textContent).toContain("failed — retry");
    expect(container.textContent).not.toContain("remove");
    expect(marks(container).length).toBe(0);
  });
});

/**
 * A landed co-performer tag. The bubble renders `releaseForms` — what OF
 * STORED — so an operator can tell a tag that stuck from one OF dropped. Until
 * 2026-08-17 the chat body sent its ids in `userTags`, which that endpoint
 * ignores, and nothing rendered the field: the send looked identical either way.
 */
describe("MessageList release-form tags", () => {
  it("names the tagged co-performer on the bubble", () => {
    const { container } = renderWithProviders(
      <MessageList
        {...baseProps}
        messages={[
          msg({
            id: 7,
            text: "collab",
            fromUser: { id: 555 },
            releaseForms: [{ id: 424242, name: "Ava Rae", type: "user", partnerSource: "tag" }],
          } as Partial<SeedMsg> & { id: number }),
        ]}
        ownerUserId={555}
      />,
    );
    expect(container.textContent).toContain("Ava Rae");
  });

  it("shows nothing when OF stored no tag, however the send was composed", () => {
    const { container } = renderWithProviders(
      <MessageList
        {...baseProps}
        messages={[msg({ id: 8, text: "collab", fromUser: { id: 555 }, releaseForms: [] })]}
        ownerUserId={555}
      />,
    );
    expect(container.querySelector("[title^='Tagged on OnlyFans']")).toBeNull();
  });
});
