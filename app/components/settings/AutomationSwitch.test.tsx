/**
 * AutomationSwitch — one box that means "this automation is scheduled".
 *
 * The info-gather chatter (welcome_chatter_for_info) had no surface but the
 * Automation rules list, so the switch that replaces that trip has to behave like
 * the editor would: create ONE rule at the catalogued cadence when the account has
 * none, wake the EXISTING row rather than adding a second ticker, and — when it
 * says off — leave nothing of that kind enabled.
 */
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from "vitest";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

vi.mock("@/lib/relay", async (importOriginal) => {
  const mod = await importOriginal<typeof import("@/lib/relay")>();
  return {
    ...mod,
    relay: { ...mod.relay, get: vi.fn(), post: vi.fn(), patch: vi.fn() },
  };
});

import {
  AutomationRuleBadge, AutomationSwitchCard,
} from "@/components/settings/AutomationSwitch";
import { relay } from "@/lib/relay";

const relayGet = relay.get as unknown as Mock;
const relayPost = relay.post as unknown as Mock;
const relayPatch = relay.patch as unknown as Mock;

const ACCT = "123456789";
const KIND = "welcome_chatter_for_info";

const KINDS = [{
  kind: KIND, label: "Get to know fans (AI info-gather)", recurring: true,
  summary: "", knobs: [], surface: "rules", cadence_default_s: 60,
}];

function rule(over: Record<string, unknown> = {}) {
  return {
    id: 7, account_id: ACCT, name: "Get to know fans (AI info-gather)", kind: KIND,
    is_enabled: false, every_seconds: 60, trigger: { every_seconds: 60 }, payload: {},
    quiet_hours: null, created_at: null, last_run: null, next_due_at: null,
    has_pending_job: false, ...over,
  };
}

/** Route the two GETs the switch makes (rules list + kind catalog). */
function serve(rules: Record<string, unknown>[]) {
  relayGet.mockImplementation((path: string) =>
    path.startsWith("/admin/automation-kinds")
      ? Promise.resolve({ kinds: KINDS })
      : Promise.resolve({ rules }));
}

let client: QueryClient;
function wrapper({ children }: { children: ReactNode }) {
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}
const card = (acct: string | null) => (
  <AutomationSwitchCard accountId={acct} kind={KIND} title="Get to know fans">
    body copy
  </AutomationSwitchCard>
);
const mount = () => render(card(ACCT), { wrapper });

beforeEach(() => {
  client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } },
  });
  relayGet.mockReset();
  relayPost.mockReset().mockResolvedValue(rule({ is_enabled: true }));
  relayPatch.mockReset().mockResolvedValue(rule({ is_enabled: true }));
});
afterEach(() => { cleanup(); });

describe("AutomationSwitchCard", () => {
  // Create-vs-wake-vs-park-them-all is the SERVER's decision now (one write, one
  // transaction — `ensure_kind_rule`, pinned by test_automation_rules_api). What
  // is left to pin here is that the box asks for the switch and nothing else: the
  // client used to hold its own copy of that policy, and two copies drift.
  it("asks the server for the switch — whatever the account's rules look like",
    async () => {
      const shapes: Record<string, unknown>[][] = [
        [],                                                     // no rule at all
        [rule({ is_enabled: false })],                           // one parked
        [rule({ id: 7, is_enabled: true }),                      // two running
         rule({ id: 9, is_enabled: true })],
      ];
      for (const rows of shapes) {
        client = new QueryClient({
          defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } },
        });
        relayPost.mockClear();
        relayPatch.mockClear();
        serve(rows);
        mount();

        const box = await screen.findByRole("checkbox") as HTMLInputElement;
        const on = rows.some((r) => Boolean(r.is_enabled));
        await waitFor(() => expect(box.disabled).toBe(false));
        await waitFor(() => expect(box.checked).toBe(on), { timeout: 2000 });

        await userEvent.click(box);
        await waitFor(() => expect(relayPost).toHaveBeenCalledTimes(1));
        expect(relayPost.mock.calls[0][0]).toBe("/admin/automation-rules/switch");
        expect(relayPost.mock.calls[0][1]).toEqual(
          { account_id: ACCT, kind: KIND, enable: !on });
        // No rule ids from the client: it does not know which row, or how many.
        expect(relayPatch).not.toHaveBeenCalled();
        cleanup();
      }
    });

  it("reads a rule as ON and shows its cadence", async () => {
    serve([rule({ is_enabled: true, every_seconds: 300 })]);
    mount();
    await screen.findByText("on · every 5 min");
  });

  it("never invents a cadence for a rule that runs on a daily clock", async () => {
    // `every_seconds: null` = a daily_at trigger. Falling back to the catalog
    // cadence here printed "every 1 min" over a rule that fires at 09:00 —
    // the switch would have been lying about the schedule it was showing.
    serve([rule({
      is_enabled: true, every_seconds: null,
      trigger: { daily_at: ["09:00"], tz_offset_minutes: 60 },
    })]);
    mount();
    await screen.findByText("on · —");
    expect(screen.queryByText(/on · every/)).toBeNull();
  });

  it("names the cadence a NEW rule would get, only while there is no rule", async () => {
    serve([]);
    mount();
    await screen.findByText(/creates the "Get to know fans \(AI info-gather\)" rule \(every 1 min\)/);
  });

  it("goes inert while a NEW account's rules are still loading", async () => {
    // The rules query keeps the previous account's rows on screen (keepPreviousData)
    // while the next account loads. Clicking in that window used to PATCH a rule id
    // belonging to the account the operator had just left.
    serve([rule({ is_enabled: true })]);
    const view = render(card(ACCT), { wrapper });
    await waitFor(() =>
      expect((screen.getByRole("checkbox") as HTMLInputElement).checked).toBe(true));

    let release: (v: unknown) => void = () => {};
    relayGet.mockImplementation((path: string) =>
      path.startsWith("/admin/automation-kinds")
        ? Promise.resolve({ kinds: KINDS })
        : new Promise((res) => { release = res; }));
    view.rerender(card("987654321"));

    // Still drawing the OLD account's "on" — so it must not be clickable.
    const box = screen.getByRole("checkbox") as HTMLInputElement;
    await waitFor(() => expect(box.disabled).toBe(true));
    await userEvent.click(box);
    expect(relayPost).not.toHaveBeenCalled();
    // …and it must not claim the account has no rule while it is still asking.
    expect(screen.queryByText(/Ticking the box creates/)).toBeNull();

    release({ rules: [] });
    await screen.findByText("not set up yet");
  });

  /** The catalog feeds only the label and the new-rule cadence, and the client
   *  no longer sends a cadence at all — the switch route reads its own catalog —
   *  so a catalog that has not answered must not hold a write against rules that
   *  HAVE been read.
   *
   *  Held open rather than rejected, deliberately: a rejected query leaves
   *  `isLoading` false immediately, so a test that rejects stays green against
   *  the old `|| kindsQ.isLoading` term and discriminates nothing. Pending is
   *  the state that tells the two predicates apart. */
  it("does not hold the switch while the kind catalog is still unanswered", async () => {
    relayGet.mockImplementation((path: string) =>
      path.startsWith("/admin/automation-kinds")
        ? new Promise(() => {})
        : Promise.resolve({ rules: [rule({ is_enabled: true })] }));
    mount();

    const box = await screen.findByRole("checkbox");
    await waitFor(() => expect((box as HTMLInputElement).disabled).toBe(false));
  });

  /** Ungating the write on the catalog opened a window the copy has to survive:
   *  rules loaded, catalog still pending, no rule of this kind. `kindLabel` and
   *  `newRuleCadence` are fallbacks there — the raw slug and 300s — so naming
   *  them describes a rule the server will not create (it uses this kind's own
   *  label and 60s). The sentence stays quiet until the catalog answers. */
  it("does not name a cadence the catalog has not confirmed", async () => {
    relayGet.mockImplementation((path: string) =>
      path.startsWith("/admin/automation-kinds")
        ? new Promise(() => {})
        : Promise.resolve({ rules: [] }));
    mount();

    await screen.findByText(/Ticking the box creates/);
    expect(screen.queryByText(/every 5 min/)).toBeNull();
    expect(screen.queryByText(new RegExp(KIND))).toBeNull();
  });

  /** A poll failing after a GOOD read is not the same as never having read. The
   *  rows are still cached and still true, so the card must keep saying what it
   *  knows; deriving the displayed STATE from the write gate blanked a healthy
   *  card — badge gone, "Checking this account's automation rules…" — on one bad
   *  request out of a 30s poll. */
  it("keeps the rule's state on screen when a later poll fails", async () => {
    serve([rule({ is_enabled: true })]);
    mount();
    await screen.findByText(/^on ·/);

    relayGet.mockImplementation((path: string) =>
      path.startsWith("/admin/automation-kinds")
        ? Promise.resolve({ kinds: KINDS })
        : Promise.reject(new Error("poll failed")));
    // Wait for the error to actually land before asserting negatives: the
    // observer notifies through a setTimeout(0), so reading straight after `act`
    // can observe the pre-error render and pass for the wrong reason.
    await act(async () => { await client.refetchQueries().catch(() => {}); });
    await waitFor(() => expect(
      client.getQueryState(["automation-rules", ACCT])?.status).toBe("error"));

    expect(screen.getByText(/^on ·/)).toBeTruthy();
    expect(screen.queryByText(/Checking this account/i)).toBeNull();
  });

  /** A read that FAILED is not a read that came back empty. Keyed on isLoading
   *  alone this card settled with no rows and reported the kind "not set up yet"
   *  over a list it never managed to read — offering a live checkbox whose click
   *  would create a second rule beside whatever is actually there. */
  it("does not report a kind as absent when the rules read failed", async () => {
    relayGet.mockImplementation((path: string) =>
      path.startsWith("/admin/automation-kinds")
        ? Promise.resolve({ kinds: KINDS })
        : Promise.reject(new Error("relay down")));
    mount();

    // Wait for the FAILURE, not just for a disabled box: on first render the box
    // is disabled because the read is still in flight, so `waitFor` would pass
    // immediately and prove nothing about the failed state.
    await waitFor(() => expect(
      client.getQueryState(["automation-rules", ACCT])?.status).toBe("error"));
    const box = await screen.findByRole("checkbox");
    expect((box as HTMLInputElement).disabled).toBe(true);
    // Not "not set up yet" — and not "still checking" either, which is the other
    // untrue thing to say once the read has come back failed.
    expect(screen.queryByText("not set up yet")).toBeNull();
    expect(screen.queryByText(/Checking this account/i)).toBeNull();
    expect(screen.getByText(/Couldn.t read this account.s automation rules/i)).toBeTruthy();
    expect(relayPost).not.toHaveBeenCalled();
    expect(relayPatch).not.toHaveBeenCalled();
  });

  it("surfaces a failed write instead of drawing the box as flipped", async () => {
    serve([]);
    relayPost.mockRejectedValue(new Error("relay 500"));
    mount();
    await screen.findByText("not set up yet");

    await userEvent.click(screen.getByRole("checkbox"));
    await screen.findByText(/relay 500 — nothing was changed\./);
    expect((screen.getByRole("checkbox") as HTMLInputElement).checked).toBe(false);
  });
});

/**
 * The header badge is the same hook with the checkbox taken away: the AI Chatter
 * tab's own "Enabled" box writes the rule now (put_ai_chatter_config), so what
 * ships beside it is a status light. Pinned because a light that can be CLICKED
 * is the duplicate-control bug coming back.
 */
describe("AutomationRuleBadge (header status light)", () => {
  it("shows the running rule's cadence and offers nothing to click", async () => {
    serve([rule({ is_enabled: true, every_seconds: 300 })]);
    render(<AutomationRuleBadge accountId={ACCT} kind={KIND} />, { wrapper });

    await screen.findByText("on · every 5 min");
    expect(screen.queryByRole("checkbox")).toBeNull();
  });

  it("names a rule that exists but is parked", async () => {
    serve([rule({ is_enabled: false })]);
    render(<AutomationRuleBadge accountId={ACCT} kind={KIND} />, { wrapper });
    await screen.findByText("rule is off");
  });

  it("writes nothing, ever", async () => {
    serve([rule({ is_enabled: true })]);
    render(<AutomationRuleBadge accountId={ACCT} kind={KIND} />, { wrapper });
    await screen.findByText(/^on ·/);
    expect(relayPatch).not.toHaveBeenCalled();
    expect(relayPost).not.toHaveBeenCalled();
  });
});
