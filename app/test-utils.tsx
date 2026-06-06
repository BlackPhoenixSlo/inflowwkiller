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
 *  (so a test can seed the cache via client.setQueryData before asserting). */
export function renderWithProviders(ui: ReactElement, client?: QueryClient) {
  const queryClient = client ?? makeTestQueryClient();
  const utils = render(
    <QueryClientProvider client={queryClient}>{ui}</QueryClientProvider>,
  );
  return { ...utils, queryClient };
}
