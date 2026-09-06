/**
 * The hook-upsell controls on the "After a buy" card.
 *
 * Three controls, and the interesting one is the NESTING. `hook_upsell_early_ask`
 * and `hook_upsell_effort` are production TEST switches (plans/hook-upsell R4-b,
 * R4-c) for a feature that ships off, so they are only meaningful once the master
 * is on — and unlike the pairing one card up, the server cannot enforce that: the
 * config endpoint replaces the whole blob and has no business refusing a
 * combination an operator might set deliberately from the raw-JSON editor.
 *
 * So the guard lives here, and it is asserted on the PAYLOAD as well as the
 * pixels. A `disabled` attribute that looks right while a stale value still
 * reaches the server is the failure that matters.
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

function configResponse(config: Record<string, unknown>) {
  return {
    account_id: "123456789",
    // The whole card is inert without the gate, so every case here turns it on.
    config: { enabled: true, qualification_gate_enabled: true, ...config },
    defaults: {},
    script_pack: {},
    starter_singles: [],
    slot_help: {},
    timezone: "UTC",
    utc_offset: 0,
  };
}

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

/** The tab renders the Save row twice — in-flow on desktop, sticky on a phone. */
async function save(user: ReturnType<typeof userEvent.setup>) {
  await user.click(
    screen.getAllByRole("button", { name: /Save Upseller settings/i })[0]);
  return (relayPut.mock.calls.at(-1)?.[1] as
    { config: Record<string, unknown> }).config;
}

const HOOK = /Upsell on what he.s doing or asking/i;
const EARLY = /Also sell on an early ask/i;
const EFFORT = /Hook read effort/i;

afterEach(() => { cleanup(); vi.clearAllMocks(); });

describe("the hook upsell card", () => {
  it("renders all three controls, and ships them off", async () => {
    relayGet.mockImplementation(async (path: string) =>
      path.includes("ai-chatter-config") ? configResponse({}) : {});
    render(<UpsellerTab accountId="123456789" />, { wrapper });

    const hook = await screen.findByRole("checkbox", { name: HOOK });
    expect(hook).not.toBeChecked();
    expect(screen.getByRole("checkbox", { name: EARLY })).not.toBeChecked();
    // "auto" is the shipped meaning of an ABSENT key, not just of a stored one —
    // the select must not render blank on an account that has never saved.
    expect(screen.getByRole("combobox", { name: EFFORT })).toHaveValue("auto");
  });

  it("the inner block is disabled while the hook is off, and live once it is on", async () => {
    relayGet.mockImplementation(async (path: string) =>
      path.includes("ai-chatter-config") ? configResponse({}) : {});
    relayPut.mockResolvedValue({ ok: true });
    const user = userEvent.setup();
    render(<UpsellerTab accountId="123456789" />, { wrapper });

    const hook = await screen.findByRole("checkbox", { name: HOOK });
    expect(screen.getByRole("checkbox", { name: EARLY })).toBeDisabled();
    expect(screen.getByRole("combobox", { name: EFFORT })).toBeDisabled();

    await user.click(hook);
    expect(screen.getByRole("checkbox", { name: EARLY })).toBeEnabled();
    expect(screen.getByRole("combobox", { name: EFFORT })).toBeEnabled();
  });

  it("each control writes its own key, and only its own", async () => {
    relayGet.mockImplementation(async (path: string) =>
      path.includes("ai-chatter-config") ? configResponse({}) : {});
    relayPut.mockResolvedValue({ ok: true });
    const user = userEvent.setup();
    render(<UpsellerTab accountId="123456789" />, { wrapper });

    // The master alone. The early-ask switch must NOT be dragged on with it —
    // three sends per purchase instead of two is not what ticking one box means.
    await user.click(await screen.findByRole("checkbox", { name: HOOK }));
    let body = await save(user);
    expect(body.hook_upsell_enabled).toBe(true);
    expect(body.hook_upsell_early_ask).toBeFalsy();

    await user.click(screen.getByRole("checkbox", { name: EARLY }));
    body = await save(user);
    expect(body.hook_upsell_enabled).toBe(true);
    expect(body.hook_upsell_early_ask).toBe(true);

    await user.selectOptions(
      screen.getByRole("combobox", { name: EFFORT }), "max");
    body = await save(user);
    expect(body.hook_upsell_effort).toBe("max");
    // …and the two booleans are untouched by the select.
    expect(body.hook_upsell_enabled).toBe(true);
    expect(body.hook_upsell_early_ask).toBe(true);
  });

  it("the stored effort round-trips into the select", async () => {
    relayGet.mockImplementation(async (path: string) =>
      path.includes("ai-chatter-config")
        ? configResponse({ hook_upsell_enabled: true, hook_upsell_effort: "high" })
        : {});
    render(<UpsellerTab accountId="123456789" />, { wrapper });

    expect(await screen.findByRole("combobox", { name: EFFORT }))
      .toHaveValue("high");
    expect(screen.getByRole("checkbox", { name: HOOK })).toBeChecked();
  });

  it("the ADD-ON control: an untouched card sends no hook keys at all", async () => {
    // The whole feature ships off, and "off" must mean the operator's blob does
    // not carry the keys — not that it carries three falses. A tab that wrote
    // defaults on every save would stamp them onto every account that ever
    // opened it, and `_validate_cfg` keeps a key iff it was actually sent.
    relayGet.mockImplementation(async (path: string) =>
      path.includes("ai-chatter-config") ? configResponse({}) : {});
    relayPut.mockResolvedValue({ ok: true });
    const user = userEvent.setup();
    render(<UpsellerTab accountId="123456789" />, { wrapper });

    await screen.findByRole("checkbox", { name: HOOK });
    const body = await save(user);
    expect(body).not.toHaveProperty("hook_upsell_enabled");
    expect(body).not.toHaveProperty("hook_upsell_early_ask");
    expect(body).not.toHaveProperty("hook_upsell_effort");
  });
});
