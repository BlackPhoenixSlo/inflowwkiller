/**
 * /messages — the Stuff page's tab strip.
 *
 * Two things here are load-bearing and neither is visible to a type check.
 *
 * The first is that CUSTOMS IS A TAB. The owed-customs queue used to be its
 * own nav entry with its own route; it is now a tab body, and the failure mode
 * of getting that wrong is silent — an unknown `?tab=` value falls back to PPV,
 * so a broken Customs tab looks exactly like a working PPV tab. These cases
 * mount the real queue through the real strip and read a row out of it.
 *
 * The second is the SHELL WIDTH. `max-w-shell` is a theme token
 * (--container-shell in globals.css); if the class is renamed on one side and
 * not the other, Tailwind emits nothing, no build fails, and the page silently
 * goes full-bleed. Asserting the class here is cheap and catches the rename.
 *
 * The heavy tabs (PPV and friends) are mounted for real rather than stubbed —
 * that is what proves the strip is wired — so this file stubs the two browser
 * APIs jsdom lacks and lets every relay call resolve empty.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

const { relayGet, params, searchParams } = vi.hoisted(() => {
  const params = { search: "" };
  let cached = { key: "\u0000", value: new URLSearchParams() };
  return {
    relayGet: vi.fn(),
    params,
    // Next hands back the SAME URLSearchParams for the life of a navigation.
    // A fresh object per render is not a harmless test shortcut: the page's
    // URL→state mirror is keyed on it (`useEffect(…, [search])`), so an
    // unstable identity re-runs it every render and stomps the tab the user
    // just clicked back to the one in the (never-updated) URL. The component
    // is fine; a naive mock is what breaks.
    searchParams: () => {
      if (cached.key !== params.search) {
        cached = { key: params.search, value: new URLSearchParams(params.search) };
      }
      return cached.value;
    },
  };
});

// The global setup pins useSearchParams to an empty string; these cases deep-
// link into a tab, which is the same door the /customs redirect comes through.
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), prefetch: vi.fn() }),
  usePathname: () => "/messages",
  useSearchParams: () => searchParams(),
  useParams: () => ({}),
}));

vi.mock("@/lib/relay", () => ({
  relay: {
    get: (...args: unknown[]) => relayGet(...args),
    post: vi.fn(async () => ({})),
  },
  proxyImage: () => "",
}));

vi.mock("@/hooks/useAccounts", () => ({
  useActiveAccounts: () => [],
  useAccounts: () => ({ data: [], isLoading: false }),
}));

import { renderWithProviders } from "@/test-utils";
import MessagesPage from "./page";

/** One owed row, shaped like /admin/customs answers it. Every identifier here
 *  is invented — this repo mirrors to a public one, so fixtures never carry a
 *  real account, fan or display name even when nothing asserts on them. */
const OWED = {
  marked: true,
  account_id: "100000001",
  account_name: "Test Creator",
  fan_id: 200000002,
  display_name: "Test Fan",
  tip_cents: 12000,
  tipped_at: "2026-08-26T00:02:46Z",
  lifetime_spend_cents: 22182,
  chat_href: "/chat/100000001/200000002",
};

beforeEach(() => {
  params.search = "";
  relayGet.mockReset();
  relayGet.mockImplementation(async (path: string) => {
    if (String(path).startsWith("/admin/customs")) {
      return { customs: [OWED], untracked: [] };
    }
    return { rows: [], items: [], next_cursor: null };
  });
  // jsdom ships neither; PaidMessagesTab's infinite-scroll sentinel and the
  // chart-ish tabs construct them on mount.
  vi.stubGlobal("IntersectionObserver", class {
    observe() {}
    unobserve() {}
    disconnect() {}
    takeRecords() { return []; }
  });
  vi.stubGlobal("ResizeObserver", class {
    observe() {}
    unobserve() {}
    disconnect() {}
  });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("the Stuff page", () => {
  it("is called Stuff and spans the shell width", () => {
    const { container } = renderWithProviders(<MessagesPage />);
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Stuff");
    expect(container.querySelector(".max-w-shell")).not.toBeNull();
  });

  it("carries Customs in the strip, between the money tabs and the feed tabs", () => {
    renderWithProviders(<MessagesPage />);
    // Scoped to the strip itself: the PPV tab's own Status filter has an "All"
    // button too, and a page-wide button sweep happily conflates the two.
    const strip = screen.getByRole("button", { name: "PPV" }).parentElement;
    const labels = Array.from(strip?.querySelectorAll("button") ?? [], (b) => b.textContent);
    expect(labels).toEqual(["PPV", "Tips", "All", "Customs", "Posts", "My Feed", "Top Posts"]);
  });

  it("opens the owed-customs queue when the tab is clicked", async () => {
    renderWithProviders(<MessagesPage />);
    await userEvent.click(screen.getByRole("button", { name: "Customs" }));

    expect(await screen.findByText("Customs owed")).toBeInTheDocument();
    // The row itself, not just the heading — the queue actually fetched.
    expect(await screen.findByText("Test Fan")).toBeInTheDocument();
    expect(relayGet).toHaveBeenCalledWith("/admin/customs");
  });

  it("deep-links straight into the queue, which is where /customs lands", async () => {
    // The 308 in next.config.ts sends /customs here. If ?tab=customs stopped
    // parsing, the redirect would quietly dump the operator on PPV.
    params.search = "tab=customs";
    renderWithProviders(<MessagesPage />);
    await waitFor(() => expect(screen.getByText("Customs owed")).toBeInTheDocument());
  });
});
