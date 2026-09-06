/**
 * The "answer a content-ask with content" switch writes BOTH flags.
 *
 * `pack_on_ask_enabled` is the trigger and `pack_send_enabled` is the master, and
 * the trigger is INERT without the master — `plan_pack`/`plan_ask` both refuse on
 * `pack_send_enabled` before anything else runs. So a tab that offered the two as
 * independent checkboxes would let an operator tick the visible one, watch it save
 * 200, and get nothing. That is the exact shape `scripts_api._validate` carries two
 * warning comments about, one level up.
 *
 * This pins the pairing at the UI, because it cannot be pinned at the server: the
 * config endpoint REPLACES the whole blob and has no business refusing a valid
 * combination an operator might set deliberately from the raw-JSON editor.
 */
import { describe, expect, it, vi, type Mock } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach } from "vitest";
import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

vi.mock("@/lib/relay", async (importOriginal) => {
  const mod = await importOriginal<typeof import("@/lib/relay")>();
  return { ...mod, relay: { ...mod.relay, get: vi.fn(), put: vi.fn() } };
});

import UpsellerTab from "@/components/settings/UpsellerTab";
import { relay } from "@/lib/relay";
import { inFlowSave as sharedInFlowSave } from "@/test-utils";

const relayGet = relay.get as unknown as Mock;
const relayPut = relay.put as unknown as Mock;

const CONFIG_RESPONSE = {
  account_id: "123456789",
  config: { enabled: true, qualification_gate_enabled: true },
  defaults: {},
  script_pack: {},
  starter_singles: [],
  slot_help: {},
  timezone: "UTC",
  utc_offset: 0,
};

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

/** These cases mean the IN-FLOW Save, not the pinned twin at the bottom of the
 *  viewport — both carry the same accessible name on purpose, so a name lookup
 *  cannot tell them apart. The shared helper lives in app/test-utils.tsx. */
const inFlowSave = () => sharedInFlowSave(/Save Upseller settings/i);

afterEach(() => { cleanup(); vi.clearAllMocks(); });

describe("answering a content-ask", () => {
  it("ticking the trigger also turns on the master switch", async () => {
    relayGet.mockImplementation(async (path: string) =>
      path.includes("ai-chatter-config") ? CONFIG_RESPONSE : {});
    relayPut.mockResolvedValue({ ok: true });
    const user = userEvent.setup();

    render(<UpsellerTab accountId="123456789" />, { wrapper });
    const box = await screen.findByRole("checkbox", {
      name: /Answer with the content, not a sentence/i,
    });
    expect(box).not.toBeChecked();
    await user.click(box);

    expect(box).toBeChecked();

    // Assert the PAYLOAD, not the pixels. A checkbox that looks ticked while the
    // master flag never reaches the server is precisely the failure this guards,
    // and only the PUT body can tell the two apart.
    await user.click(inFlowSave());
    const body = relayPut.mock.calls.at(-1)?.[1] as { config: Record<string, unknown> };
    expect(body.config.pack_on_ask_enabled).toBe(true);
    expect(body.config.pack_send_enabled).toBe(true);
  });

  it("the widened readers are on by default, and the tease rate is sent as a fraction", async () => {
    // 🚨 THE UNIT MISMATCH THIS EXISTS TO CATCH. The operator types a PERCENT and
    // the engine reads a 0..1 FRACTION. Send 33 instead of 0.33 and the server's
    // clamp turns it into 1.0 — every tease-yes sells, which is the opposite of
    // the ruling and would look like a working save.
    relayGet.mockImplementation(async (path: string) =>
      path.includes("ai-chatter-config") ? CONFIG_RESPONSE : {});
    relayPut.mockResolvedValue({ ok: true });
    const user = userEvent.setup();

    render(<UpsellerTab accountId="123456789" />, { wrapper });
    // ON by default: the stored blob above carries neither key, and the box must
    // still read ticked — `!!cfg.x` would render it OFF and an operator ticking it
    // "on" would be writing a no-op.
    const wide = await screen.findByRole("checkbox", {
      name: /Count "how much\?" and "yes" as asking/i,
    });
    expect(wide).toBeChecked();

    // …but inert until the master trigger is on, like everything else in this
    // card — so the operator's real path is to tick that first.
    const rate = screen.getByRole("spinbutton", { name: /sell on his yes this often/i });
    expect(rate).toBeDisabled();
    await user.click(await screen.findByRole("checkbox", {
      name: /Answer with the content, not a sentence/i,
    }));
    expect(rate).toBeEnabled();
    expect(rate).toHaveValue(33);          // the shipped default, as a percentage
    await user.clear(rate);
    await user.type(rate, "50");

    await user.click(inFlowSave());
    const body = relayPut.mock.calls.at(-1)?.[1] as { config: Record<string, unknown> };
    expect(body.config.tease_sell_rate).toBe(0.5);
  });

  it("the rate-card veto is unreachable until the trigger is on", async () => {
    relayGet.mockImplementation(async (path: string) =>
      path.includes("ai-chatter-config") ? CONFIG_RESPONSE : {});
    render(<UpsellerTab accountId="123456789" />, { wrapper });
    // A knob that silently does nothing is worse than a missing one: the price
    // ceiling only exists inside a pack, so it is disabled until packs can send.
    const veto = await screen.findByRole("checkbox", {
      name: /Never charge above the content/i,
    });
    expect(veto).toBeDisabled();
  });
});
