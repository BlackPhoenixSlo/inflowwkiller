/**
 * useScrollStrip — the two behaviours a horizontal strip does NOT get for
 * free from the browser: wheel→sideways (a mouse only emits deltaY, so
 * without it the tail of an overflowing strip is unreachable) and keeping
 * the active child in view when the route/tab changes.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render } from "@testing-library/react";

import { useScrollStrip } from "./useScrollStrip";

/** jsdom lays nothing out: scrollWidth/clientWidth are always 0. Pin them
 *  so "does this strip actually overflow?" is a decision the test controls. */
function setWidths(el: HTMLElement, scrollWidth: number, clientWidth: number) {
  Object.defineProperty(el, "scrollWidth", { value: scrollWidth, configurable: true });
  Object.defineProperty(el, "clientWidth", { value: clientWidth, configurable: true });
}

function wheel(el: HTMLElement, deltaY: number, deltaX = 0, deltaMode = 0) {
  const e = new WheelEvent("wheel", { deltaY, deltaX, deltaMode, bubbles: true, cancelable: true });
  el.dispatchEvent(e);
  return e;
}

function Strip({ activeKey, activeIndex = 0 }: { activeKey: string; activeIndex?: number }) {
  const { stripRef, activeRef } = useScrollStrip<HTMLAnchorElement>(activeKey);
  return (
    <nav ref={stripRef} data-testid="strip">
      {["a", "b"].map((k, i) => (
        <a key={k} href={`/${k}`} ref={i === activeIndex ? activeRef : undefined} data-testid={`link-${k}`}>
          {k}
        </a>
      ))}
    </nav>
  );
}

// jsdom does no layout, so it ships no scrollIntoView at all.
beforeEach(() => {
  Element.prototype.scrollIntoView = vi.fn();
});

describe("useScrollStrip — wheel maps onto scrollLeft", () => {
  it("an overflowing strip scrolls sideways and swallows the event", () => {
    const { getByTestId } = render(<Strip activeKey="/a" />);
    const strip = getByTestId("strip");
    setWidths(strip, 900, 400);

    const e = wheel(strip, 120);

    expect(strip.scrollLeft).toBe(120);
    expect(e.defaultPrevented).toBe(true);
  });

  it("a strip that fits is left alone — no scroll, and the page keeps the event", () => {
    const { getByTestId } = render(<Strip activeKey="/a" />);
    const strip = getByTestId("strip");
    setWidths(strip, 400, 400);

    const e = wheel(strip, 120);

    expect(strip.scrollLeft).toBe(0);
    expect(e.defaultPrevented).toBe(false);
  });

  it("a LINE-mode delta (Firefox) is normalised to px, not taken raw", () => {
    const { getByTestId } = render(<Strip activeKey="/a" />);
    const strip = getByTestId("strip");
    setWidths(strip, 900, 400);

    wheel(strip, 3, 0, 1); // three lines, not three pixels

    expect(strip.scrollLeft).toBe(48);
  });

  it("a genuine sideways gesture (trackpad) is left to the browser", () => {
    const { getByTestId } = render(<Strip activeKey="/a" />);
    const strip = getByTestId("strip");
    setWidths(strip, 900, 400);

    const e = wheel(strip, 5, 80);

    expect(strip.scrollLeft).toBe(0);
    expect(e.defaultPrevented).toBe(false);
  });

  it("unmount detaches the listener — the ref cleanup, not an effect", () => {
    const { getByTestId, unmount } = render(<Strip activeKey="/a" />);
    const strip = getByTestId("strip");
    setWidths(strip, 900, 400);
    unmount();

    const e = wheel(strip, 120);

    expect(strip.scrollLeft).toBe(0);
    expect(e.defaultPrevented).toBe(false);
  });
});

describe("useScrollStrip — the active child stays in view", () => {
  it("pulls the active link into view on mount and on every key change", () => {
    const spy = vi.spyOn(Element.prototype, "scrollIntoView");
    const { rerender, getByTestId } = render(<Strip activeKey="/a" activeIndex={0} />);
    expect(spy.mock.instances[0]).toBe(getByTestId("link-a"));

    rerender(<Strip activeKey="/b" activeIndex={1} />);

    expect(spy).toHaveBeenCalledTimes(2);
    expect(spy.mock.instances[1]).toBe(getByTestId("link-b"));
    expect(spy.mock.calls[1][0]).toEqual({ block: "nearest", inline: "nearest" });
  });

  it("re-rendering without a key change does not yank the strip around", () => {
    const spy = vi.spyOn(Element.prototype, "scrollIntoView");
    const { rerender } = render(<Strip activeKey="/a" />);
    rerender(<Strip activeKey="/a" />);

    expect(spy).toHaveBeenCalledTimes(1);
  });
});
