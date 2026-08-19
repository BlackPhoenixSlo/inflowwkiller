"use client";

import { useCallback, useEffect, useRef } from "react";

/**
 * A wheel delta is in pixels, LINES, or PAGES depending on the browser/OS
 * (`WheelEvent.deltaMode`). Firefox reports lines, where one notch is ~3 —
 * added raw, the strip would creep 3px per notch. Indexed by deltaMode:
 * DOM_DELTA_PIXEL, DOM_DELTA_LINE, DOM_DELTA_PAGE.
 */
const PX_PER_DELTA = [1, 16, 400];

/**
 * A row of links/chips that scrolls sideways once its children outgrow the
 * space it has — the top-nav strip, tab rows, filter-chip strips. Adds the
 * two things the browser does NOT give a horizontal scroller for free:
 *
 *  - A mouse wheel only emits deltaY, so on a plain mouse (no trackpad)
 *    the tail of an overflowing strip is unreachable. Map deltaY onto
 *    scrollLeft.
 *  - Pull the active child into view whenever `activeKey` changes.
 *    Landing on a route whose chip sits past the fold otherwise reads as
 *    "the strip lost my page".
 *
 * Hang `stripRef` on the scroller (the element carrying `overflow-x-auto`)
 * and `activeRef` on whichever child is currently active:
 *
 *   const { stripRef, activeRef } = useScrollStrip<HTMLAnchorElement>(pathname);
 *   <nav ref={stripRef} className="flex overflow-x-auto no-scrollbar">
 *     {links.map((l) => {
 *       const active = l.href === pathname;
 *       return <Link key={l.href} ref={active ? activeRef : undefined} … />;
 *     })}
 *   </nav>
 *
 * `A` is the active child's element type (Next's `Link` forwards to an
 * anchor, a tab row to a button) — pass it so no cast is needed at the
 * call site.
 */
export function useScrollStrip<A extends HTMLElement = HTMLElement>(activeKey: string | null) {
  const activeRef = useRef<A | null>(null);

  // A ref callback rather than an effect: the listener follows the element
  // itself, so a strip that mounts late or conditionally can never end up
  // with a handler bound to nothing. Non-passive because we preventDefault
  // — React's own onWheel is passive, where preventDefault is a no-op and
  // the page would scroll vertically at the same time.
  const stripRef = useCallback((el: HTMLElement | null) => {
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      if (el.scrollWidth <= el.clientWidth) return;           // nothing to scroll
      if (Math.abs(e.deltaY) <= Math.abs(e.deltaX)) return;   // real sideways gesture — the browser has it
      el.scrollLeft += e.deltaY * (PX_PER_DELTA[e.deltaMode] ?? 1);
      e.preventDefault();
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, []);

  useEffect(() => {
    activeRef.current?.scrollIntoView({ block: "nearest", inline: "nearest" });
  }, [activeKey]);

  return { stripRef, activeRef };
}
