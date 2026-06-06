/**
 * MediaTile — reserved skeleton box (T0 / DA bug 10).
 *
 * The placeholder used to be a bare <div> with no reserved height, so the
 * box was ~0px until the image loaded and then grew, shoving the text
 * below. These tests pin the fix:
 *   • pre-load: the skeleton reserves a non-zero min-height and uses NO
 *     hard aspect-ratio (aspect-[4/5] would letterbox wide images).
 *   • post-load: the image fills the same box with object-cover, so the
 *     swap causes zero vertical movement.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, waitFor } from "@testing-library/react";

import { MediaTile } from "@/components/chat/MediaTile";

// jsdom lacks IntersectionObserver; a no-op stub keeps the lazy (eager:false)
// path from ever calling start(), so the component stays in its skeleton
// state for the pre-load assertion.
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
  cleanup();
  vi.unstubAllGlobals();
  // Drop the object-URL shims we attach in the load test.
  delete (URL as unknown as { createObjectURL?: unknown }).createObjectURL;
  delete (URL as unknown as { revokeObjectURL?: unknown }).revokeObjectURL;
});

describe("MediaTile skeleton", () => {
  it("reserved-box: the skeleton reserves a min-height and uses no hard aspect ratio", () => {
    const { container } = render(
      <MediaTile
        mediaId={1}
        proxiedUrl="https://cdn.test/a.jpg"
        fallbackUrl="https://cdn.test/a.jpg"
        eager={false}
        className="rounded-md border border-black/10"
      />,
    );

    const ph = container.firstElementChild as HTMLElement;
    // Pre-load it's the skeleton div, not an <img>.
    expect(ph.tagName).toBe("DIV");
    expect(container.querySelector("img")).toBeNull();
    // Non-zero reserved height (a min-h-[…] floor), never a 0-height box.
    expect(ph.className).toMatch(/min-h-\[/);
    // Must NOT pin a hard aspect-ratio (that letterboxes wide images and
    // just moves the reflow).
    expect(ph.className).not.toContain("aspect-[4/5]");
  });

  it("reserved-box: once loaded, the image fills the box with object-cover", async () => {
    const blob = new Blob(["x"], { type: "image/jpeg" });
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: true,
        status: 200,
        headers: { get: () => null },
        blob: async () => blob,
      })),
    );
    (URL as unknown as { createObjectURL: () => string }).createObjectURL = () => "blob:obj-1";
    (URL as unknown as { revokeObjectURL: (u: string) => void }).revokeObjectURL = () => {};

    const { container } = render(
      <MediaTile
        mediaId={2}
        proxiedUrl="https://cdn.test/b.jpg"
        fallbackUrl="https://cdn.test/b.jpg"
        eager
        alt="photo"
        className="rounded-md border border-black/10"
      />,
    );

    const img = await waitFor(() => {
      const el = container.querySelector("img");
      if (!el) throw new Error("img not yet rendered");
      return el as HTMLImageElement;
    });

    expect(img.getAttribute("src")).toBe("blob:obj-1");
    // Image fills the reserved box; crop (cover), not letterbox.
    expect(img.className).toContain("object-cover");
    expect(img.className).not.toContain("aspect-[4/5]");
  });
});
