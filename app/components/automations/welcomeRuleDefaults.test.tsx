/**
 * A WELCOME RULE CREATED FROM THE BRAIN PANEL SHIPS THE ON-BY-DEFAULT KNOBS ON.
 *
 * `human_pace` and `stop_on_reply` were flipped ON by default (operator,
 * 2026-09-06): the send_welcome catalog declares `default: True`, the sender's
 * `_on_unless_off` treats an absent key as on, and the panel's EXISTING-rule seed
 * reads `!== false`. The NO-RULE seed did not get the memo — it set both to
 * `false`, the create payload wrote whatever that state held, and
 * `_validate_payload_for_kind` faithfully preserved it.
 *
 * The failure was silent and it inverted the decision for exactly the accounts it
 * mattered most for: an operator opening Brain on an account with no welcome rule
 * saw both boxes UNTICKED, pressed "Enable welcome", and got a rule pinned to
 * `human_pace: false, stop_on_reply: false` — while the same rule created through
 * the typed rules editor inherited the catalog's `True`. Nothing was red.
 *
 * So this asserts BOTH halves, because either alone passes with the bug in one of
 * its two forms: what the operator SEES before saving (the checkboxes), and what
 * the rule is actually BORN with (the POST body).
 */
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

vi.mock("@/lib/relay", async (importOriginal) => {
  const mod = await importOriginal<typeof import("@/lib/relay")>();
  // The spread hands out the REAL method for anything not stubbed here, so a
  // future edit that reaches a new verb fails loudly instead of talking to a
  // live relay (same discipline as brainLane.test.tsx).
  return {
    ...mod,
    relay: {
      ...mod.relay,
      get: vi.fn(), put: vi.fn(), post: vi.fn(), patch: vi.fn(),
      delete: vi.fn(), uploadFile: vi.fn(),
    },
  };
});
vi.mock("@/hooks/useAccounts", () => ({
  useActiveAccounts: () => [{ id: "2024813", username: "lucas" }],
}));
vi.mock("@/components/chat/VaultPicker", () => ({ VaultPicker: () => null }));
vi.mock("@/hooks/useVaultMediaByIds", () => ({ useVaultMediaByIds: () => ({}) }));

import BrainPanel from "@/components/automations/BrainPanel";
import { relay } from "@/lib/relay";

const relayGet = relay.get as unknown as Mock;
const relayPost = relay.post as unknown as Mock;

const ACC = "2024813";

const ACCOUNT_CONFIG = {
  account_id: ACC,
  config: { voice: "her", persona: "", persona_facts: {}, time_activities: {},
            time_images: {}, model_by_purpose: {}, language: "en", location: null,
            timezone: null, utc_offset: 0, daily_cost_cap_cents: 100, model: null },
  defaults: { persona: "" },
  defaults_by_voice: { her: { persona: "" }, him: { persona: "" } },
  persona_fact_fields: [], persona_fact_fields_by_voice: { her: [], him: [] },
  slots: [], model_options: [], purposes: [], languages: [{ code: "en", label: "English" }],
};

let client: QueryClient;

beforeEach(() => {
  client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } },
  });
  relayGet.mockReset();
  relayPost.mockReset();
  relayPost.mockResolvedValue({ id: 1, kind: "send_welcome" });
});
afterEach(() => cleanup());

/** Mount the REAL panel on an account whose rules list is EMPTY — the no-rule
 *  branch, which is the only branch that can create a welcome rule. */
const mountWithNoWelcomeRule = async () => {
  relayGet.mockImplementation((path: string) => {
    const p = String(path);
    if (p.startsWith("/admin/account-config")) return Promise.resolve(ACCOUNT_CONFIG);
    if (p.startsWith("/admin/automation-rules")) return Promise.resolve({ rules: [] });
    return Promise.resolve({});
  });
  render(<QueryClientProvider client={client}><BrainPanel /></QueryClientProvider>);
  // The button says "Enable welcome" precisely when no rule exists.
  await waitFor(() => expect(screen.getByText("Enable welcome")).toBeTruthy());
};

const boxFor = (label: string): HTMLInputElement => {
  const text = screen.getByText(label);
  return text.closest("label")!.querySelector("input[type=checkbox]") as HTMLInputElement;
};

describe("a welcome rule created from the Brain panel", () => {
  it("shows the on-by-default knobs TICKED before anything is saved", async () => {
    await mountWithNoWelcomeRule();
    // What the operator reads off the screen has to be what he is about to get.
    expect(boxFor("Pace it like a person").checked).toBe(true);
    expect(boxFor("Stop if he replies mid-welcome").checked).toBe(true);
    expect(boxFor("Short 2nd bubble (time & place only)").checked).toBe(true);
    expect(boxFor("Skip the time bubble").checked).toBe(false);
    // ⚠️ MONEY. The price-check went ON by default 2026-09-07 ("we always skip
    // subscribers who charge to follow"), and an unticked box here is an
    // operator being told this account will follow priced creators blind.
    expect(boxFor("Follow back after they subscribe").checked).toBe(true);
    expect(boxFor("Skip subscribers who charge to follow").checked).toBe(true);
  });

  it("POSTs human_pace and stop_on_reply as TRUE", async () => {
    await mountWithNoWelcomeRule();
    await act(async () => { fireEvent.click(screen.getByText("Enable welcome")); });

    await waitFor(() => expect(relayPost).toHaveBeenCalled());
    const call = relayPost.mock.calls.find(
      (c) => String(c[0]) === "/admin/automation-rules");
    expect(call).toBeTruthy();
    const draft = call![1] as { kind: string; payload: Record<string, unknown> };
    expect(draft.kind).toBe("send_welcome");
    // The two the operator flipped on by default. `false` here is the bug.
    expect(draft.payload.human_pace).toBe(true);
    expect(draft.payload.stop_on_reply).toBe(true);
    // ...and the other two shape knobs, so the whole created shape is pinned.
    expect(draft.payload.time_only).toBe(true);
    expect(draft.payload.skip_time_bubble).toBe(false);
    // ⚠️ MONEY, and the one knob here whose wrong value costs real cash: a rule
    // born with `follow_back_gate: false` buys a subscription to every priced
    // creator who subscribes to it.
    expect(draft.payload.follow_back).toBe(true);
    expect(draft.payload.follow_back_gate).toBe(true);
  });

  it("still honours a box the operator UNticks before creating the rule", async () => {
    // The defaults must be a starting point, not a floor: untick means off, and
    // an explicit `false` is what turns the sender's absent-means-on off.
    await mountWithNoWelcomeRule();
    await act(async () => { fireEvent.click(boxFor("Pace it like a person")); });
    await act(async () => { fireEvent.click(screen.getByText("Enable welcome")); });

    await waitFor(() => expect(relayPost).toHaveBeenCalled());
    const draft = relayPost.mock.calls.find(
      (c) => String(c[0]) === "/admin/automation-rules")![1] as
        { payload: Record<string, unknown> };
    expect(draft.payload.human_pace).toBe(false);
    expect(draft.payload.stop_on_reply).toBe(true);
  });

  it("lets the operator untick the money gate and writes the explicit false", async () => {
    // The opt-out has to survive to the payload as `false`, not as an absent
    // key: the sender's read-default is now ON, so omitting it would silently
    // re-arm the gate on a rule the operator deliberately opened up.
    await mountWithNoWelcomeRule();
    await act(async () => {
      fireEvent.click(boxFor("Skip subscribers who charge to follow"));
    });
    await act(async () => { fireEvent.click(screen.getByText("Enable welcome")); });

    await waitFor(() => expect(relayPost).toHaveBeenCalled());
    const draft = relayPost.mock.calls.find(
      (c) => String(c[0]) === "/admin/automation-rules")![1] as
        { payload: Record<string, unknown> };
    expect(draft.payload.follow_back_gate).toBe(false);
    expect(draft.payload.follow_back).toBe(true);
  });
});
