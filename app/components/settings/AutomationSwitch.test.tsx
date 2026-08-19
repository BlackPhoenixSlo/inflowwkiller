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
import { cleanup, render, screen, waitFor } from "@testing-library/react";
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
  AutomationSwitch, AutomationSwitchCard,
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
  it("creates the rule at the catalogued cadence when the account has none", async () => {
    serve([]);
    mount();
    await screen.findByText("not set up yet");
    const box = screen.getByRole("checkbox");
    expect((box as HTMLInputElement).checked).toBe(false);

    await userEvent.click(box);
    await waitFor(() => expect(relayPost).toHaveBeenCalledTimes(1));
    const [path, body] = relayPost.mock.calls[0] as [string, Record<string, unknown>];
    expect(path).toBe("/admin/automation-rules");
    expect(body).toMatchObject({
      account_id: ACCT, kind: KIND, every_seconds: 60, is_enabled: true, payload: {},
    });
  });

  it("wakes the existing parked rule instead of adding a second ticker", async () => {
    serve([rule({ is_enabled: false })]);
    mount();
    await screen.findByText("rule is off");

    await userEvent.click(screen.getByRole("checkbox"));
    await waitFor(() => expect(relayPatch).toHaveBeenCalledTimes(1));
    expect(relayPatch.mock.calls[0][0]).toBe("/admin/automation-rules/7");
    expect(relayPatch.mock.calls[0][1]).toEqual({ is_enabled: true });
    // A second row of the same kind would just double the sweep.
    expect(relayPost).not.toHaveBeenCalled();
  });

  it("off parks EVERY enabled rule of the kind, not just the first", async () => {
    serve([rule({ id: 7, is_enabled: true }), rule({ id: 9, is_enabled: true })]);
    mount();
    await waitFor(() =>
      expect((screen.getByRole("checkbox") as HTMLInputElement).checked).toBe(true));

    await userEvent.click(screen.getByRole("checkbox"));
    await waitFor(() => expect(relayPatch).toHaveBeenCalledTimes(2));
    const ids = relayPatch.mock.calls.map((c) => c[0]);
    expect(ids).toEqual(["/admin/automation-rules/7", "/admin/automation-rules/9"]);
    expect(relayPatch.mock.calls.every((c) => c[1].is_enabled === false)).toBe(true);
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
    expect(relayPatch).not.toHaveBeenCalled();
    // …and it must not claim the account has no rule while it is still asking.
    expect(screen.queryByText(/Ticking the box creates/)).toBeNull();

    release({ rules: [] });
    await screen.findByText("not set up yet");
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
 * The header pill is the same hook in a one-line dress — but it is the copy that
 * actually ships in the AI Chatter tab header, so the wiring gets its own check
 * rather than riding on the card's.
 */
describe("AutomationSwitch (header pill)", () => {
  it("reflects the running rule and parks it on click", async () => {
    serve([rule({ is_enabled: true, every_seconds: 300 })]);
    render(<AutomationSwitch accountId={ACCT} kind={KIND} />, { wrapper });

    await screen.findByText("on · every 5 min");
    const box = screen.getByRole("checkbox") as HTMLInputElement;
    await waitFor(() => expect(box.checked).toBe(true));

    await userEvent.click(box);
    await waitFor(() => expect(relayPatch).toHaveBeenCalledTimes(1));
    expect(relayPatch.mock.calls[0][1]).toEqual({ is_enabled: false });
  });

  it("stays inert with no account picked", async () => {
    serve([]);
    render(<AutomationSwitch accountId={null} kind={KIND} />, { wrapper });
    const box = screen.getByRole("checkbox") as HTMLInputElement;
    expect(box.disabled).toBe(true);
    await userEvent.click(box);
    expect(relayPost).not.toHaveBeenCalled();
  });
});
