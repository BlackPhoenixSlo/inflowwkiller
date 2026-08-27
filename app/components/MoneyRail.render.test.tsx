/**
 * MoneyRail — the surface gate.
 *
 * The rail is mounted once in the ROOT layout, so it is on every route, but
 * `settings.surfaces` decides where it appears (house default: the chat
 * surfaces only). That test used to live in the panel's own `return null`,
 * BELOW three `useQueries` blocks — which stopped the paint but not the
 * fetches, so every admin page paid ~14 OF round-trips and rendered nothing
 * with them. It now lives in a parent that conditionally mounts the panel, so
 * these cases assert on the boundary that makes the leak impossible rather
 * than on three `enabled:` flags that a fourth query could forget.
 *
 * MoneyRail.test.ts covers the pure helpers and cannot see any of this.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, screen, waitFor } from "@testing-library/react";

// vi.mock factories are hoisted above the imports below, so everything they
// close over has to be hoisted with them or it is still in its TDZ when the
// mocked module is first imported.
const { relayGet, route, accounts } = vi.hoisted(() => ({
  relayGet: vi.fn(),
  route: { pathname: "/inbox" },
  accounts: [
    { id: "1001", nickname: "ava" },
    { id: "1002", nickname: "the second account" },
  ],
}));

// The global setup pins usePathname to "/"; these cases move between surfaces.
vi.mock("next/navigation", () => ({
  usePathname: () => route.pathname,
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), prefetch: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
  useParams: () => ({}),
}));

vi.mock("@/lib/relay", () => ({
  relay: { get: (...args: unknown[]) => relayGet(...args) },
  proxyImage: () => "",
}));

vi.mock("@/hooks/useAccounts", () => ({
  useActiveAccounts: () => accounts,
}));

import { renderWithProviders } from "@/test-utils";
import {
  DEFAULT_SETTINGS,
  SETTINGS_KEY,
  writeSettings,
  type NotificationItem,
  type RailSettings,
} from "@/lib/moneyRailStorage";
import { MoneyRail, MoneyRailRestoreButton } from "./MoneyRail";

/** The two OF round-trips the rail can make. The attribution query goes to
 *  our own SQLite (`/admin/messages/...`) — relay-local, not OF, so it is
 *  never counted as OF load. */
const OF_NOTIFICATIONS = "/api/of/v2/users/notifications";
const OF_CHAT_DETAIL = "/api/of/v2/chats/";

/** 2 accounts × {purchases, tip}. */
const FANOUT = accounts.length * 2;

function callsTo(prefix: string): string[] {
  return relayGet.mock.calls.map((c) => String(c[0])).filter((p) => p.startsWith(prefix));
}

function notif(id: number, name: string, amount: string): NotificationItem {
  return {
    id,
    type: "purchase",
    text: `${name} has purchased your message for $${amount}!`,
    createdAt: "2026-08-25T10:00:00.000Z",
    user: { id: 9000 + id, name },
  };
}

/** Three purchases on the first account, none on the second — enough rows to
 *  overflow a collapsed window, and a second account so the fan-out shows up
 *  in the call count. */
const ROWS: Record<string, NotificationItem[]> = {
  "1001:purchases": [notif(1, "Jim", "25.22"), notif(2, "Dana", "144.00"), notif(3, "Joe", "12.00")],
};

function seedRelay(): void {
  relayGet.mockImplementation((path: string, opts?: { accountId?: string }) => {
    if (path.startsWith(OF_NOTIFICATIONS)) {
      const type = new URLSearchParams(path.split("?")[1] ?? "").get("type") ?? "";
      return Promise.resolve({ list: ROWS[`${opts?.accountId}:${type}`] ?? [] });
    }
    if (path.startsWith(OF_CHAT_DETAIL)) return Promise.resolve({});
    return Promise.resolve({ by_msg_id: {} });
  });
}

/**
 * One macrotask turn: long enough for the settings effect to commit, the gate
 * to mount the panel, and TanStack to dispatch every armed queryFn.
 *
 * A "nothing was fetched" assertion can't be waited FOR, only waited OUT, so
 * it needs a wait that is provably long enough. This is that wait, and the
 * visible-surface case below asserts the full fan-out has ALREADY landed
 * after exactly one turn — so if this ever stops being sufficient, that case
 * goes red rather than the hidden-surface case going quietly green.
 */
async function flush(): Promise<void> {
  await act(async () => {
    await new Promise((r) => setTimeout(r, 0));
  });
}

beforeEach(() => {
  window.localStorage.clear();
  relayGet.mockReset();
  seedRelay();
  route.pathname = "/inbox";
  writeSettings(DEFAULT_SETTINGS);
});

afterEach(cleanup);

describe("MoneyRail surface gate", () => {
  it("issues no requests at all on a surface it is not shown on", async () => {
    // House default is ["inbox","popup","group"]; /stats maps to "other".
    route.pathname = "/stats";
    const { container } = renderWithProviders(<MoneyRail />);
    await flush();

    // Not just the OF calls — the panel never mounts, so nothing it declares
    // (queries, effects, window listeners, the query-cache subscription) runs.
    expect(relayGet).not.toHaveBeenCalled();
    expect(container).toBeEmptyDOMElement();
  });

  it("fans out within one turn on a surface it IS shown on", async () => {
    route.pathname = "/inbox";
    renderWithProviders(<MoneyRail />);
    await flush();

    // Calibrates flush() for the hidden-surface case above — see its comment.
    expect(callsTo(OF_NOTIFICATIONS)).toHaveLength(FANOUT);
    expect(await screen.findByText("Jim")).toBeInTheDocument();
    // The per-fan detail fan-out is derived from those rows, so it only starts
    // once they land.
    await waitFor(() => expect(callsTo(OF_CHAT_DETAIL)).not.toHaveLength(0));
  });

  it("keeps rendering smallRows while COLLAPSED on a visible surface", async () => {
    // The regression this guards against: gating on `dock.open` as well would
    // leave the collapsed dock with a header and an empty body. Collapsed is
    // a smaller WINDOW onto the feed, not a truncation of it.
    writeSettings({ ...DEFAULT_SETTINGS, smallRows: 2 });
    route.pathname = "/inbox";
    const { container } = renderWithProviders(<MoneyRail />);

    // Dock defaults to collapsed, so this is the collapsed path.
    expect(await screen.findByTitle("Expand")).toBeInTheDocument();
    expect(callsTo(OF_NOTIFICATIONS)).toHaveLength(FANOUT);
    const body = await waitFor(() => {
      const el = container.querySelector<HTMLElement>(".overflow-y-auto");
      expect(el).not.toBeNull();
      return el!;
    });
    expect(body.style.maxHeight).toBe("136px"); // 2 rows × ROW_H
    // Every row stays in the DOM behind that window, including the third.
    expect(screen.getByText("Jim")).toBeInTheDocument();
    expect(screen.getByText("Joe")).toBeInTheDocument();
  });

  it("rides a client-side nav between two visible surfaces, then leaves", async () => {
    // The gate decides per render, so a nav is the one thing that could have
    // started remounting the panel (and re-fetching) on every route change.
    // Between two VISIBLE surfaces it must be the same panel throughout.
    route.pathname = "/inbox";
    const { rerender, container } = renderWithProviders(<MoneyRail />);
    expect(await screen.findByText("Jim")).toBeInTheDocument();
    const fetched = callsTo(OF_NOTIFICATIONS).length;

    route.pathname = "/group"; // its own saved dock spot, same shared cache
    await act(async () => {
      rerender(<MoneyRail />);
    });
    expect(screen.getByText("Jim")).toBeInTheDocument();
    expect(callsTo(OF_NOTIFICATIONS)).toHaveLength(fetched);

    route.pathname = "/stats"; // an admin page — now it does go away
    await act(async () => {
      rerender(<MoneyRail />);
    });
    expect(container).toBeEmptyDOMElement();
  });

  it("mounts and fetches when the surface is ticked on AFTER mount", async () => {
    // The bell's restore button writes settings from outside the rail.
    route.pathname = "/stats";
    renderWithProviders(<MoneyRail />);
    await flush();
    expect(relayGet).not.toHaveBeenCalled();

    await act(async () => {
      writeSettings({ ...DEFAULT_SETTINGS, surfaces: null }); // null = everywhere
      await new Promise((r) => setTimeout(r, 0));
    });

    await waitFor(() => expect(callsTo(OF_NOTIFICATIONS)).toHaveLength(FANOUT));
    expect(await screen.findByText("Jim")).toBeInTheDocument();
  });
});

describe("MoneyRailRestoreButton", () => {
  const LABEL = /rail is hidden here/i;

  it("offers the way back only where the rail is actually hidden", async () => {
    route.pathname = "/stats"; // "other" — hidden under the house default
    const hidden = renderWithProviders(<MoneyRailRestoreButton />);
    expect(await screen.findByText(LABEL)).toBeInTheDocument();
    hidden.unmount();

    route.pathname = "/inbox"; // shown here — nothing to restore
    const { container } = renderWithProviders(<MoneyRailRestoreButton />);
    await flush();
    expect(container).toBeEmptyDOMElement();
  });

  it("follows a hide performed in ANOTHER tab", async () => {
    // Both readers of these settings share one subscription hook now. The
    // button used to roll its own, listening for the same-tab custom event
    // but never for `storage` — so a popout's bell went on claiming the rail
    // was visible here after the main tab hid it.
    route.pathname = "/inbox";
    renderWithProviders(<MoneyRailRestoreButton />);
    await flush();
    expect(screen.queryByText(LABEL)).toBeNull();

    await act(async () => {
      // Another tab's write: localStorage already holds the new value and the
      // browser delivers `storage`, but never the same-tab custom event.
      const next: RailSettings = { ...DEFAULT_SETTINGS, surfaces: ["group"] };
      window.localStorage.setItem(SETTINGS_KEY, JSON.stringify(next));
      window.dispatchEvent(new StorageEvent("storage", { key: SETTINGS_KEY }));
    });

    expect(await screen.findByText(LABEL)).toBeInTheDocument();
  });
});
