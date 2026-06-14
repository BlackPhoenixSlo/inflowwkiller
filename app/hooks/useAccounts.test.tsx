/**
 * useAccounts — per-principal cache isolation (cross-tenant leak fix).
 *
 * /admin/accounts returns the FULL multi-tenant registry when called
 * UNauthenticated (the dev-curl fallback). The owner query used to cache
 * under a principal-agnostic key `["accounts"]` with a 3-day staleTime, so
 * a list fetched in an unauthed window (app boot before /auth/me resolves,
 * a brief session expiry) persisted and bled into the next owner's
 * session — surfacing another owner's models in this owner's picker, then
 * 403'ing on save ("account X is not one of yours").
 *
 * The fix keys the cache by `user.user_id` and refuses to fetch until a
 * principal is known. These tests assert both guarantees: switching
 * principals never serves the previous user's cached accounts, and a null
 * user never triggers the unauthed /admin/accounts fetch.
 */
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from "vitest";
import { cleanup, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

vi.mock("@/lib/relay", () => ({ relay: { get: vi.fn() } }));

// useUser / useChatter are driven by mutable module-level state so each test
// (and each rerender) can flip the active principal.
let mockUser: { user_id: string; username: string } | null = null;
let mockChatter: unknown = null;
vi.mock("@/contexts/UserContext", () => ({
  useUser: () => ({ user: mockUser, loading: false }),
}));
vi.mock("@/contexts/ChatterContext", () => ({
  useChatter: () => ({ chatter: mockChatter }),
}));

import { useAccounts } from "@/hooks/useAccounts";
import { relay } from "@/lib/relay";
const relayGet = relay.get as unknown as Mock;

let client: QueryClient;
function wrapper({ children }: { children: ReactNode }) {
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

beforeEach(() => {
  client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  relayGet.mockReset();
  mockUser = null;
  mockChatter = null;
});
afterEach(() => { cleanup(); });

describe("useAccounts principal isolation", () => {
  it("does NOT fetch /admin/accounts before a principal is known", async () => {
    // No user, no chatter (app boot / login screen). Pre-fix this fired an
    // unauthed /admin/accounts → full registry → poisoned cache.
    renderHook(() => useAccounts(), { wrapper });
    await new Promise((r) => setTimeout(r, 0));
    expect(relayGet).not.toHaveBeenCalled();
  });

  it("never serves one owner's cached accounts to the next owner", async () => {
    relayGet
      .mockResolvedValueOnce({ accounts: [{ id: "acct-a" }], active_account_id: "acct-a" })
      .mockResolvedValueOnce({ accounts: [{ id: "acct-b" }], active_account_id: "acct-b" });

    mockUser = { user_id: "owner-A", username: "owner-a" };
    const { result, rerender } = renderHook(() => useAccounts(), { wrapper });
    await waitFor(() => expect(result.current.data?.accounts?.[0]?.id).toBe("acct-a"));

    // Flip to a different signed-in owner on the same browser/cache.
    mockUser = { user_id: "owner-B", username: "owner-b" };
    rerender();

    // Must refetch under the new principal's key — not reuse owner-A's
    // (still-fresh, 3-day staleTime) cache entry.
    await waitFor(() => expect(result.current.data?.accounts?.[0]?.id).toBe("acct-b"));
    expect(relayGet).toHaveBeenCalledTimes(2);
  });
});
