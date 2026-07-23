/**
 * chatPopout — deep-link helpers for the money surfaces (Buys & tips rail,
 * tip/purchase toasts) that open a fan's chat in a popout tab.
 *
 * One NAMED browsing context per (account, fan): re-clicking the same fan
 * re-navigates the existing popout — a full reload, so the persisted cache
 * restores and the head fetch + `?refresh=media` one-shot run again —
 * instead of stacking another `_blank` tab that paints the stale
 * localStorage snapshot until its first fetch lands. The anchors using
 * these helpers deliberately DROP rel="noreferrer": noopener semantics
 * (implied by noreferrer) make browsers treat any named target as `_blank`,
 * which is exactly the tab-per-click behavior being killed — and these are
 * same-origin links, so there's no opener/referrer exposure to defend
 * against.
 */

import type { MouseEvent } from "react";

/** Stable window name for a fan-chat href — query stripped so the
 *  `?refresh=media` variant and a plain link share the same tab. */
export function chatTabName(href: string): string {
  return `fastt-${href.split("?")[0]}`;
}

/** Left-click handler: navigate the named tab via window.open so we get
 *  the WindowProxy back and can focus() it — browsers re-navigate a named
 *  target in the background otherwise. Modified clicks (cmd/ctrl/shift/alt,
 *  non-left buttons) fall through to native anchor behavior. */
export function openChatTab(e: MouseEvent, href: string): void {
  if (e.defaultPrevented || e.button !== 0) return;
  if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
  e.preventDefault();
  window.open(href, chatTabName(href))?.focus();
}
