/**
 * Shared test helpers. Chat components read the TanStack Query cache, so they
 * must render under a QueryClientProvider — use renderWithProviders() instead
 * of RTL's bare render(). See library/TEST_PLAN.md.
 *
 * Requires the T-TESTKIT dev-deps (@testing-library/react, @tanstack/react-query).
 */
import type { ReactElement } from "react";
import { render } from "@testing-library/react";
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
