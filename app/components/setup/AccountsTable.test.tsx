/**
 * AccountsTable — dropping a model evicts ALL of its cached data.
 *
 * The delete used to only `invalidateQueries(["accounts"])`, leaving 30+
 * per-account React Query keys (chats, vault-media, of-user, fan-*,
 * *-config/*-rules, automation-*, nudge-*) + the persisted localStorage
 * snapshot holding the dropped account's data until staleTime expiry — so a
 * manual logout/login was the only way to clear it.
 *
 * The fix runs `removeQueries` with a predicate that matches the dropped
 * accountId anywhere in a query key (including the unified "all"-scope chat
 * key, whose accountKey is a comma-joined list of every fanned-out id). These
 * cases assert: the dropped account's per-account caches are HARD-removed,
 * another account's caches survive untouched, and the accounts list refetches
 * (the deleted row disappears).
 */
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from "vitest";
import { cleanup, render, screen, waitFor, fireEvent } from "@testing-library/react";
import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

vi.mock("@/lib/relay", () => ({ relay: { get: vi.fn(), delete: vi.fn() } }));

import AccountsTable from "@/components/setup/AccountsTable";
import { relay } from "@/lib/relay";
const relayGet = relay.get as unknown as Mock;
const relayDelete = relay.delete as unknown as Mock;

let client: QueryClient;
function wrapper({ children }: { children: ReactNode }) {
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

beforeEach(() => {
  client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } });
  relayGet.mockReset();
  relayDelete.mockReset();
  // Auto-confirm the window.confirm() in the Delete handler.
  vi.stubGlobal("confirm", vi.fn(() => true));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("AccountsTable delete — full per-account cache eviction", () => {
  it("hard-removes every per-account query for the dropped account and spares others", async () => {
    let accountsCalls = 0;
    relayGet.mockImplementation((path: string) => {
      if (path === "/admin/accounts") {
        accountsCalls += 1;
        // First load lists both; the post-delete refetch drops acct-del.
        return Promise.resolve(
          accountsCalls === 1
            ? { accounts: [{ id: "acct-del" }, { id: "acct-keep" }], active_account_id: "acct-keep" }
            : { accounts: [{ id: "acct-keep" }], active_account_id: "acct-keep" },
        );
      }
      if (path.startsWith("/health")) return Promise.resolve({ ok: true, accounts: [] });
      return Promise.resolve({});
    });
    relayDelete.mockResolvedValue({ ok: true });

    // Seed the cache the way the live app would — per-account keys for BOTH
    // accounts plus the unified "all"-scope chat key (comma-joined ids).
    client.setQueryData(["chats", "model", "acct-del", null, null, null, 30], "del-chats");
    client.setQueryData(["chats", "model", "acct-keep", null, null, null, 30], "keep-chats");
    client.setQueryData(["chats", "all", "acct-del,acct-keep", null, null, null, 30], "combined-chats");
    client.setQueryData(["vault-media", "acct-del"], "del-vault");
    client.setQueryData(["vault-media", "acct-keep"], "keep-vault");
    client.setQueryData(["of-user", "acct-del", "fan1"], "del-ofuser");
    // A key with a non-string part — predicate must not throw on .split().
    client.setQueryData(["fan", "acct-del", 12345], "del-fan");

    render(<AccountsTable />, { wrapper });

    // Wait for the table to paint both rows (the id shows in two cells —
    // the name button + the user_id column — so match on the Delete buttons).
    await waitFor(() =>
      expect(screen.getAllByRole("button", { name: "Delete" })).toHaveLength(2),
    );

    // Two rows, two Delete buttons; row order follows the accounts array, so
    // index 0 is acct-del.
    const deleteButtons = screen.getAllByRole("button", { name: "Delete" });
    fireEvent.click(deleteButtons[0]);

    // The DELETE relay call goes out for the right account.
    await waitFor(() =>
      expect(relayDelete).toHaveBeenCalledWith("/admin/accounts/acct-del"),
    );

    // The dropped account's per-account caches are HARD-gone...
    await waitFor(() => {
      expect(client.getQueryData(["chats", "model", "acct-del", null, null, null, 30])).toBeUndefined();
    });
    expect(client.getQueryData(["vault-media", "acct-del"])).toBeUndefined();
    expect(client.getQueryData(["of-user", "acct-del", "fan1"])).toBeUndefined();
    expect(client.getQueryData(["fan", "acct-del", 12345])).toBeUndefined();
    // ...including the unified key whose accountKey lists the dropped id.
    expect(client.getQueryData(["chats", "all", "acct-del,acct-keep", null, null, null, 30])).toBeUndefined();

    // The OTHER account's caches survive untouched.
    expect(client.getQueryData(["chats", "model", "acct-keep", null, null, null, 30])).toBe("keep-chats");
    expect(client.getQueryData(["vault-media", "acct-keep"])).toBe("keep-vault");

    // The accounts list refetched and the deleted row is gone.
    await waitFor(() => expect(screen.queryByText("acct-del")).not.toBeInTheDocument());
    expect(screen.getAllByText("acct-keep").length).toBeGreaterThanOrEqual(1);
    expect(accountsCalls).toBeGreaterThanOrEqual(2);
  });
});

/**
 * A dead OF session pauses EVERY automation for that account
 * (service/account_health.py), but the relay's `session_dead_at` stamp had no
 * reader — so twelve accounts sat in the fleet with rules showing as enabled
 * while nothing ran, indistinguishable from accounts that were merely quiet.
 * These cases pin the badge that closes that gap.
 */
describe("AccountsTable — a paused account says it is paused", () => {
  it("badges the dead account, spares the healthy one, and names the repair", async () => {
    relayGet.mockImplementation((path: string) => {
      if (path === "/admin/accounts") {
        return Promise.resolve({
          accounts: [
            {
              id: "acct-dead",
              has_session: true,
              session_dead_at: "2026-07-10T14:14:37Z",
              session_dead_reason: "no_session",
            },
            { id: "acct-live", has_session: true },
          ],
          active_account_id: "acct-live",
        });
      }
      if (path.startsWith("/health")) {
        // OF answers the health probe for BOTH. The pause is a separate fact
        // from liveness — an account can pass this probe while its automations
        // stay parked — so a green probe must NOT hide the badge.
        return Promise.resolve({
          ok: true,
          accounts: [
            { account_id: "acct-dead", ok: true, name: "Dead" },
            { account_id: "acct-live", ok: true, name: "Live" },
          ],
        });
      }
      return Promise.resolve({});
    });

    render(<AccountsTable />, { wrapper });

    await waitFor(() =>
      expect(screen.getByText("automations paused")).toBeInTheDocument(),
    );
    // Exactly one row is badged — the healthy account is untouched.
    expect(screen.getAllByText("automations paused")).toHaveLength(1);
    // The operator is told WHICH repair, not shown the raw enum.
    expect(screen.getByTitle(/captured in Setup/i)).toBeInTheDocument();
    expect(screen.queryByText("no_session")).not.toBeInTheDocument();
    // …and how long it has been down, so a month-dead account is obvious.
    expect(screen.getByText(/^unlinked /)).toBeInTheDocument();
  });

  it("leaves a fleet with no dead sessions completely unbadged", async () => {
    relayGet.mockImplementation((path: string) => {
      if (path === "/admin/accounts") {
        return Promise.resolve({
          accounts: [{ id: "acct-live", has_session: true }],
          active_account_id: "acct-live",
        });
      }
      if (path.startsWith("/health")) {
        return Promise.resolve({
          ok: true,
          accounts: [{ account_id: "acct-live", ok: true, name: "Live" }],
        });
      }
      return Promise.resolve({});
    });

    render(<AccountsTable />, { wrapper });

    // The id shows in two cells (name button + user_id column), so wait on the
    // row's Delete button instead — same reason as the delete case above.
    await waitFor(() =>
      expect(screen.getAllByRole("button", { name: "Delete" })).toHaveLength(1),
    );
    expect(screen.queryByText("automations paused")).not.toBeInTheDocument();
    expect(screen.queryByText(/^unlinked /)).not.toBeInTheDocument();
  });
});
