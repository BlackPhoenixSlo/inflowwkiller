/**
 * MediaStrip tile geometry (T-GRID).
 *
 * Media renders as small fixed-footprint thumbnails. Every tile's box is
 * computed up-front by mediaTileBox() from the payload's width/height (NOT
 * from <img> onload), and the image plus every placeholder (lock /
 * processing / skeleton) read the SAME box — so the async byte swap never
 * reflows. These tests pin the pure size function and the rendered inline
 * box, including the null-dimension fallback (OF didn't size it) and the
 * processing-placeholder-matches-image invariant.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent } from "@testing-library/react";

import { renderWithProviders } from "@/test-utils";
import { MessageList, isPortraitMedia, mediaTileBox } from "@/components/chat/MessageList";
import type { OFMedia, OFMessage } from "@/lib/relay";

// jsdom lacks IntersectionObserver (MediaTile's lazy-load path) — no-op stub
// so the lazy tiles stay in their skeleton state.
class IOStub {
  observe() {}
  unobserve() {}
  disconnect() {}
  takeRecords(): unknown[] {
    return [];
  }
}

const baseProps = {
  accountId: "acct1",
  ownerUserId: null,
  isLoading: false,
  isError: false,
  hasOlder: false,
  loadingOlder: false,
  onLoadOlder: () => {},
  onRetry: () => {},
};

function media(over: Partial<OFMedia> & { id: number }): OFMedia {
  return {
    type: "photo",
    files: { thumb: { url: `https://cdn.test/${over.id}.jpg` } },
    ...over,
  } as OFMedia;
}

/** One incoming message (so no vault query / no PPV badges) carrying media. */
function withMedia(items: OFMedia[]): OFMessage {
  return {
    id: 1,
    text: "",
    fromUser: { id: 42 },
    createdAt: "2026-06-04T12:00:00.000Z",
    media: items,
  } as OFMessage;
}

beforeEach(() => {
  window.localStorage.clear();
  vi.stubGlobal("IntersectionObserver", IOStub);
  // The most-recent message's first media fetches eagerly; keep that fetch
  // pending forever so the tile stays a skeleton. We only assert on the <a>
  // wrapper + its inline box, both of which render synchronously.
  vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})));
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("isPortraitMedia", () => {
  it("tall media (≥1.2× as high as wide) is portrait", () => {
    expect(isPortraitMedia(800, 1200)).toBe(true); // 1.5
    expect(isPortraitMedia(1000, 1200)).toBe(true); // exactly at the threshold
  });
  it("landscape / square / near-square media is not portrait", () => {
    expect(isPortraitMedia(1200, 800)).toBe(false); // wide
    expect(isPortraitMedia(1000, 1000)).toBe(false); // square
    expect(isPortraitMedia(1000, 1100)).toBe(false); // 1.1 < threshold
  });
  it("null / zero / negative dims fall back to not-portrait", () => {
    expect(isPortraitMedia(null, 1200)).toBe(false);
    expect(isPortraitMedia(800, null)).toBe(false);
    expect(isPortraitMedia(undefined, undefined)).toBe(false);
    expect(isPortraitMedia(0, 1200)).toBe(false);
    expect(isPortraitMedia(-5, 1200)).toBe(false);
  });
});

describe("mediaTileBox", () => {
  it("is always a square sized only by the compact toggle", () => {
    expect(mediaTileBox(true)).toEqual({ w: 64, h: 64 });
    expect(mediaTileBox(false)).toEqual({ w: 155, h: 155 });
  });
  it("ignores the media's width/height (the box must NOT depend on async dims)", () => {
    // Same square no matter the orientation — landscape, portrait, square, or
    // unknown all reserve the identical box, so a late dims PATCH can't reflow.
    expect(mediaTileBox(false, 1200, 800)).toEqual({ w: 155, h: 155 }); // wide
    expect(mediaTileBox(false, 800, 1200)).toEqual({ w: 155, h: 155 }); // tall
    expect(mediaTileBox(false, null, null)).toEqual({ w: 155, h: 155 }); // unknown
    expect(mediaTileBox(true, 800, 1200)).toEqual({ w: 64, h: 64 }); // compact, tall
    expect(mediaTileBox(true, null, null)).toEqual({ w: 64, h: 64 }); // compact, unknown
  });
});

describe("MediaStrip tile box", () => {
  function anchors(container: HTMLElement): HTMLAnchorElement[] {
    return Array.from(container.querySelectorAll("a"));
  }

  it("tiles wrap in a flex row (no fixed-row grid)", () => {
    const { container } = renderWithProviders(
      <MessageList
        {...baseProps}
        messages={[withMedia([media({ id: 100, width: 1200, height: 800 })])]}
      />,
    );
    const strip = container.querySelector(".flex-wrap");
    expect(strip).toBeTruthy();
  });

  it("every tile is the SAME fixed square regardless of orientation (compact default = 64)", () => {
    const { container } = renderWithProviders(
      <MessageList
        {...baseProps}
        messages={[
          withMedia([
            media({ id: 100, width: 1200, height: 800 }), // landscape
            media({ id: 200, width: 800, height: 1200 }), // portrait
          ]),
        ]}
      />,
    );
    const tiles = anchors(container);
    expect(tiles).toHaveLength(2);
    for (const tile of tiles) {
      expect(tile.style.width).toBe("64px");
      expect(tile.style.height).toBe("64px");
    }
  });

  it("null dimensions reserve the same square (no throw, no dependence on dims)", () => {
    const { container } = renderWithProviders(
      <MessageList
        {...baseProps}
        messages={[withMedia([media({ id: 300, width: null, height: null })])]}
      />,
    );
    const [tile] = anchors(container);
    expect(tile).toBeTruthy();
    expect(tile.style.width).toBe("64px");
    expect(tile.style.height).toBe("64px");
  });

  it("a still-processing tile reserves the SAME square the loaded image will (no reflow on load)", () => {
    // No files → no thumb yet → the placeholder. It must claim the same box
    // the image will, so the swap doesn't shove content.
    const { container } = renderWithProviders(
      <MessageList
        {...baseProps}
        messages={[withMedia([{ id: 400, type: "photo", width: 800, height: 1200 } as OFMedia])]}
      />,
    );
    // No <a> (no thumb) — the placeholder is a div carrying the box.
    const ph = container.querySelector('div[style*="width"]') as HTMLElement;
    expect(ph).toBeTruthy();
    expect(ph.style.width).toBe("64px");
    expect(ph.style.height).toBe("64px");
  });
});

describe("MediaLightbox (click-to-zoom)", () => {
  it("left-clicking a tile pops the full-res overlay; backdrop click closes it", () => {
    const { container } = renderWithProviders(
      <MessageList
        {...baseProps}
        messages={[withMedia([media({ id: 100, width: 1200, height: 800 })])]}
      />,
    );
    fireEvent.click(container.querySelector("a")!);

    // Overlay is portaled to <body>, so query the document, not the container.
    const dialog = document.querySelector('[role="dialog"]') as HTMLElement;
    expect(dialog).toBeTruthy();
    expect(dialog.querySelector("img")).toBeTruthy();

    // Clicking the backdrop (the dialog root) closes it.
    fireEvent.click(dialog);
    expect(document.querySelector('[role="dialog"]')).toBeNull();
  });

  it("clicking the image itself does NOT close the overlay", () => {
    const { container } = renderWithProviders(
      <MessageList
        {...baseProps}
        messages={[withMedia([media({ id: 100, width: 1200, height: 800 })])]}
      />,
    );
    fireEvent.click(container.querySelector("a")!);
    const img = document.querySelector('[role="dialog"] img') as HTMLElement;
    fireEvent.click(img);
    expect(document.querySelector('[role="dialog"]')).toBeTruthy();
  });
});
