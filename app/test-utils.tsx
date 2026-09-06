/**
 * Shared test helpers. Chat components read the TanStack Query cache, so they
 * must render under a QueryClientProvider — use renderWithProviders() instead
 * of RTL's bare render(). See library/TEST_PLAN.md.
 *
 * Requires the T-TESTKIT dev-deps (@testing-library/react, @tanstack/react-query).
 */
import type { ReactElement } from "react";
import { expect } from "vitest";
import { render, screen, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

export function makeTestQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0, staleTime: 0 },
      mutations: { retry: false },
    },
  });
}

/** Render a component with a fresh QueryClient. Returns RTL utils + the client
 *  (so a test can seed the cache via client.setQueryData before asserting).
 *
 *  `rerender` is WRAPPED. RTL's own rerender replaces the whole tree with what
 *  you hand it, so calling it with a bare element silently drops the provider
 *  and the component dies on "No QueryClient set" — which reads like a bug in
 *  the component rather than in the test. Any suite that needs to assert on a
 *  props CHANGE (an optimistic row patched in place, say) needs this. */
export function renderWithProviders(ui: ReactElement, client?: QueryClient) {
  const queryClient = client ?? makeTestQueryClient();
  const wrap = (node: ReactElement) => (
    <QueryClientProvider client={queryClient}>{node}</QueryClientProvider>
  );
  const utils = render(wrap(ui));
  return {
    ...utils,
    rerender: (next: ReactElement) => utils.rerender(wrap(next)),
    queryClient,
  };
}

/* ── the automation tabs' two Save controls ─────────────────────────────── */

/** An automation tab renders its Save TWICE: once in flow, and once in the
 *  pinned bar at the bottom of the viewport (components/settings/StickySaveBar).
 *  Both carry the SAME accessible name on purpose — the pin is the same control,
 *  not a second one — so a role+name lookup cannot tell them apart and
 *  `getAllByRole(...)[0]` silently follows whichever the DOM happens to emit
 *  first. These two say which one they mean, and they live here rather than in
 *  each suite because three suites had already copied the same eight lines.
 *
 *  `toHaveLength(1)` is load-bearing, not tidiness: it fails loudly the day a
 *  third control answers to that name, instead of quietly testing the wrong
 *  button. */
export function inFlowSave(name: RegExp | string): HTMLElement {
  const bar = screen.queryByTestId("sticky-save-bar");
  const hit = screen
    .getAllByRole("button", { name })
    .filter((b) => !bar || !bar.contains(b));
  expect(hit).toHaveLength(1);
  return hit[0];
}

/** The pinned bar, and the Save inside it. */
export function pinnedSave(name: RegExp | string): {
  bar: HTMLElement;
  button: HTMLElement;
} {
  const bar = screen.getByTestId("sticky-save-bar");
  return { bar, button: within(bar).getByRole("button", { name }) };
}
