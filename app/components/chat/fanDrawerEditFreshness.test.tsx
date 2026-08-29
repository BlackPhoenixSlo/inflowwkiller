/**
 * FanDrawer — the drawer's nickname/note pair is the ONLY place an `of-user`
 * value is read back out and written over, which makes it the only place
 * staleness can destroy data rather than merely look old.
 *
 * `useOFUser` no longer refetches on mount (a live OF /users/{id} on the user
 * lane, paid on every drawer open, for data that barely moves). That leaves a
 * window: a nickname/note changed elsewhere inside the 5-minute staleTime
 * would prefill stale, get typed over, and save — silently reverting the
 * other change. Window focus only covers tab switches, not a same-tab reopen.
 *
 * These tests pin the contract that closes it:
 *   • merely OPENING the drawer still costs no OF read,
 *   • ENGAGING an edit affordance re-reads, exactly once per open,
 *   • a fresh value that lands before typing replaces the prefill,
 *   • a fresh value that lands AFTER typing began does not clobber the input,
 *   • a PINNED drawer (open all day) re-reads again once the stale window
 *     passes — the guard is time-based, not once-per-open.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const { relayGet } = vi.hoisted(() => ({ relayGet: vi.fn() }));
vi.mock("@/lib/relay", () => ({
  relay: { get: relayGet, post: vi.fn(), put: vi.fn(), patch: vi.fn(), del: vi.fn() },
  proxyImage: (u: string) => u,
  proxyScrubFrame: (u: string) => u,
}));

const { update } = vi.hoisted(() => ({
  update: { mutate: vi.fn(), isPending: false },
}));
const FAN = { id: 1, tags: [] as string[], custom_nickname: null, notes: null };
vi.mock("@/hooks/useFan", () => ({
  useFan: () => ({ data: FAN, isLoading: false, update }),
}));
vi.mock("@/hooks/useAccounts", () => ({ useAccountLabel: () => "acct-a" }));
vi.mock("@/hooks/useMassExclude", () => ({
  useMassExclude: () => ({ isOn: false, isLoading: false, toggle: vi.fn() }),
}));
vi.mock("@/hooks/useLastPurchases", () => ({
  useFanActivity: () => ({ lastPurchase: new Map(), recentSpend: new Map() }),
}));
vi.mock("@/hooks/useFanPpvHistory", () => ({
  useFanPpvHistory: () => ({
    data: { items: [], summary: { max_cents: 0, avg_cents: 0 } },
    isLoading: false,
  }),
}));
vi.mock("@/hooks/useFanChatMedia", () => ({
  useFanChatMedia: () => ({ data: { pages: [] }, isLoading: false, fetchNextPage: vi.fn(), hasNextPage: false }),
}));
vi.mock("@/hooks/useCatalog", () => ({
  useAiChatterSessions: () => ({ data: [] }),
  useCancelOffer: () => ({ mutate: vi.fn(), isPending: false }),
}));
vi.mock("./PersonaClaims", () => ({ PersonaClaims: () => null }));

import { FanDrawer } from "./FanDrawer";

const CHAT = {
  withUser: { id: 7, name: "Fan Seven", username: "fan7", avatar: null, displayName: "", notice: "" },
} as never;

/** of-user responses, one per relay call, so a test can hand the SECOND read
 *  a value someone else changed while the drawer sat open. */
function queueOFUser(...bodies: Record<string, unknown>[]) {
  relayGet.mockReset();
  let i = 0;
  relayGet.mockImplementation((path: string) => {
    if (!path.includes("/users/")) return Promise.resolve({});
    const body = bodies[Math.min(i, bodies.length - 1)];
    i += 1;
    return Promise.resolve(body);
  });
}

function ofReads() {
  return relayGet.mock.calls.filter(([p]) => String(p).includes("/users/")).length;
}

let qc: QueryClient;
function Wrap({ children }: { children: ReactNode }) {
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

function renderDrawer() {
  return render(
    <FanDrawer open onClose={vi.fn()} accountId="acct-a" fanId={7} chat={CHAT} />,
    { wrapper: Wrap },
  );
}

beforeEach(() => {
  qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  update.mutate.mockReset();
  queueOFUser({ id: 7, displayName: "from OF", notice: "note from OF" });
});
afterEach(() => { cleanup(); qc.clear(); });

const nick = () => screen.getByPlaceholderText("Fan Seven") as HTMLInputElement;
const note = () =>
  screen.getByPlaceholderText(/loves bondage/) as HTMLTextAreaElement;

describe("FanDrawer edit-freshness", () => {
  it("opening the drawer on a cached fan costs no OF read", async () => {
    // Seed the cache the way a drawer opened minutes ago would have left it.
    qc.setQueryData(["of-user", "acct-a", 7], { id: 7, displayName: "cached", notice: "" });
    renderDrawer();
    await waitFor(() => expect(nick().value).toBe("cached"));
    // The whole point of `refetchOnMount: false` — a mount must not spend the
    // user lane. Give any stray fetch a chance to land before asserting.
    await new Promise((r) => setTimeout(r, 20));
    expect(ofReads()).toBe(0);
  });

  it("engaging the nickname field re-reads, and picks up a value changed elsewhere", async () => {
    const user = userEvent.setup();
    qc.setQueryData(["of-user", "acct-a", 7], { id: 7, displayName: "stale", notice: "" });
    queueOFUser({ id: 7, displayName: "changed elsewhere", notice: "" });
    renderDrawer();
    await waitFor(() => expect(nick().value).toBe("stale"));

    await user.click(nick());
    await waitFor(() => expect(nick().value).toBe("changed elsewhere"));
    expect(ofReads()).toBe(1);
  });

  it("re-reads once per open, not on every focus/blur cycle", async () => {
    const user = userEvent.setup();
    qc.setQueryData(["of-user", "acct-a", 7], { id: 7, displayName: "stale", notice: "" });
    renderDrawer();
    await waitFor(() => expect(nick().value).toBe("stale"));

    await user.click(nick());
    await waitFor(() => expect(ofReads()).toBe(1));
    await user.click(note());   // second affordance, same open
    await user.click(nick());   // back again
    await new Promise((r) => setTimeout(r, 20));
    expect(ofReads()).toBe(1);
  });

  it("re-reads again once the stale window passes (pinned drawer, open all day)", async () => {
    // Pinned/popout mode never closes, so a once-per-open guard would let a
    // value renamed at minute 50 prefill stale forever. The guard must be
    // the query's own stale window, not the drawer's open lifecycle.
    const user = userEvent.setup();
    qc.setQueryData(["of-user", "acct-a", 7], { id: 7, displayName: "stale", notice: "" });
    queueOFUser(
      { id: 7, displayName: "read one", notice: "" },
      { id: 7, displayName: "renamed while pinned", notice: "" },
    );
    renderDrawer();
    await waitFor(() => expect(nick().value).toBe("stale"));

    await user.click(nick());
    await waitFor(() => expect(nick().value).toBe("read one"));
    await user.click(note()); // same window: still one read
    expect(ofReads()).toBe(1);

    // The drawer sits open past the stale window; the fan is renamed elsewhere.
    const realNow = Date.now();
    const nowSpy = vi.spyOn(Date, "now").mockImplementation(() => realNow + 6 * 60_000);
    try {
      await user.click(nick());
      await waitFor(() => expect(nick().value).toBe("renamed while pinned"));
      expect(ofReads()).toBe(2);
    } finally {
      nowSpy.mockRestore();
    }
  });

  it("does not clobber what the operator has already typed", async () => {
    const user = userEvent.setup();
    qc.setQueryData(["of-user", "acct-a", 7], { id: 7, displayName: "stale", notice: "" });
    // Deliberately slow read: the operator gets to type before it lands.
    let release!: (v: unknown) => void;
    relayGet.mockReset();
    relayGet.mockImplementation((path: string) =>
      String(path).includes("/users/")
        ? new Promise((res) => { release = res; })
        : Promise.resolve({}),
    );
    renderDrawer();
    await waitFor(() => expect(nick().value).toBe("stale"));

    await user.click(nick());
    await user.clear(nick());
    await user.type(nick(), "operator typed");
    release({ id: 7, displayName: "arrived late", notice: "" });

    await new Promise((r) => setTimeout(r, 20));
    expect(nick().value).toBe("operator typed");
  });
});
