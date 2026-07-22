/**
 * useSellerConfig — a config that never LOADED must never be SAVED.
 *
 * Both seller tabs (🤖 AI Chatter, 💰 AI Upseller) post the FULL sparse config
 * and the server REPLACES the whole `ai_chatter_config_json` blob with it. With
 * no loaded config there is nothing to be sparse against: every key looks like a
 * default, so the build collapses to the two authoritative keys alone —
 * `{sleep_window: null, script_pack_overrides: {}}`. Pressing Save on a tab whose
 * config request failed therefore wiped the account's real settings, while the UI
 * showed the hardcoded fallbacks (`cfg.sla_minutes ?? 10`) as if they were saved.
 *
 * These tests pin the fix: `configLoaded` is false on a failed load and `saveCfg`
 * is a no-op, and a loaded config still saves through unchanged.
 */
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from "vitest";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

vi.mock("@/lib/relay", async (importOriginal) => {
  const mod = await importOriginal<typeof import("@/lib/relay")>();
  return { ...mod, relay: { ...mod.relay, get: vi.fn(), put: vi.fn() } };
});

import { useSellerConfig } from "@/components/settings/sellerShared";
import { relay } from "@/lib/relay";

const relayGet = relay.get as unknown as Mock;
const relayPut = relay.put as unknown as Mock;

/** A minimal /admin/ai-chatter-config body — the shape the hook reads. */
const CONFIG_RESPONSE = {
  account_id: "123456789",
  config: { enabled: true, sla_minutes: 45, max_lifetime_spend_cents: 250000 },
  defaults: { enabled: false, sla_minutes: 10, max_lifetime_spend_cents: 100000 },
  script_pack: {},
  timezone: "Europe/Ljubljana",
  utc_offset: 0,
  tz_offset_minutes: 60,
  derived_sleep_window: ["03:00", "10:00"],
  effective_sleep_window: ["03:00", "10:00"],
  default_sleep_window: ["03:00", "10:00"],
};

let client: QueryClient;
function wrapper({ children }: { children: ReactNode }) {
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

/** Let the mutation's own microtasks run, so "never called" isn't just "not yet". */
const flush = () => new Promise((r) => setTimeout(r, 0));

beforeEach(() => {
  client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } },
  });
  relayGet.mockReset();
  relayPut.mockReset();
  relayPut.mockResolvedValue({ ok: true });
});
afterEach(() => { cleanup(); });

describe("useSellerConfig save gating", () => {
  it("does NOT save when the config request failed", async () => {
    relayGet.mockRejectedValue(new Error("relay 500"));
    const { result } = renderHook(() => useSellerConfig("123456789"), { wrapper });

    await waitFor(() => expect(result.current.cfgQ.isError).toBe(true));
    expect(result.current.configLoaded).toBe(false);

    // Pre-fix this posted {sleep_window: null, script_pack_overrides: {}} over
    // the stored blob — a "save" that deleted the account's settings.
    await act(async () => { result.current.saveCfg(); await flush(); });
    expect(relayPut).not.toHaveBeenCalled();
  });

  it("does NOT save a one-click preset either (Enable Upseller)", async () => {
    relayGet.mockRejectedValue(new Error("relay 500"));
    const { result } = renderHook(() => useSellerConfig("123456789"), { wrapper });

    await waitFor(() => expect(result.current.cfgQ.isError).toBe(true));
    await act(async () => {
      result.current.saveCfg({ enabled: true, upsell_takes_over: true });
      await flush();
    });
    expect(relayPut).not.toHaveBeenCalled();
  });

  it("saves the loaded config through once it HAS loaded", async () => {
    relayGet.mockResolvedValue(CONFIG_RESPONSE);
    const { result } = renderHook(() => useSellerConfig("123456789"), { wrapper });

    await waitFor(() => expect(result.current.configLoaded).toBe(true));
    await act(async () => { result.current.saveCfg({ sla_minutes: 30 }); await flush(); });

    await waitFor(() => expect(relayPut).toHaveBeenCalledTimes(1));
    const [path, body] = relayPut.mock.calls[0] as [string, Record<string, unknown>];
    expect(path).toBe("/admin/ai-chatter-config");
    const cfg = body.config as Record<string, unknown>;
    // The patch, plus the stored non-default keys — not a blob of defaults.
    expect(cfg.sla_minutes).toBe(30);
    expect(cfg.enabled).toBe(true);
    expect(cfg.max_lifetime_spend_cents).toBe(250000);
    expect(body.timezone).toBe("Europe/Ljubljana");
  });
});
