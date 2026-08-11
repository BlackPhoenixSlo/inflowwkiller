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
    // The tab renders the Save row twice — in-flow on desktop, sticky on a
    // phone — so this takes the first rather than asserting there is one.
    await user.click(
      screen.getAllByRole("button", { name: /Save Upseller settings/i })[0]);
    const body = relayPut.mock.calls.at(-1)?.[1] as { config: Record<string, unknown> };
    expect(body.config.pack_on_ask_enabled).toBe(true);
    expect(body.config.pack_send_enabled).toBe(true);
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
