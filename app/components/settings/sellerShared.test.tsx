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
import { act, cleanup, render, renderHook, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

vi.mock("@/lib/relay", async (importOriginal) => {
  const mod = await importOriginal<typeof import("@/lib/relay")>();
  return { ...mod, relay: { ...mod.relay, get: vi.fn(), put: vi.fn() } };
});

import { ScriptPackCard, useSellerConfig } from "@/components/settings/sellerShared";
import { relay } from "@/lib/relay";

const relayGet = relay.get as unknown as Mock;
const relayPut = relay.put as unknown as Mock;

/** A minimal /admin/ai-chatter-config body — the shape the hook reads. */
const CONFIG_RESPONSE = {
  account_id: "123456789",
  config: { enabled: true, sla_minutes: 45, max_lifetime_spend_cents: 250000 },
  defaults: { enabled: false, sla_minutes: 10, max_lifetime_spend_cents: 100000 },
  script_pack: {},
  starter_singles: [],
  slot_help: {},
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
    // The clock is NOT written from this tab. It used to post one, which made this a
    // second writer of the same column the Brain owns — two controls, two clock
    // columns, and the silent one won (an Eastern creator ran on a Los Angeles zone).
    // The endpoint leaves an absent key unchanged, so omitting it is the fix.
    expect(body).not.toHaveProperty("timezone");
  });

  it("still sends `enabled` when it is switched OFF", async () => {
    // Sparse drops any key equal to its default, and `false` IS the default — so
    // this key used to vanish from the payload the moment you unticked the box.
    // The save endpoint syncs the automation RULE from it and only acts on a key
    // it actually receives, so a dropped `false` stored "off" and left the engine
    // ticking: the split the one-checkbox change exists to close.
    relayGet.mockResolvedValue(CONFIG_RESPONSE);
    const { result } = renderHook(() => useSellerConfig("123456789"), { wrapper });

    await waitFor(() => expect(result.current.configLoaded).toBe(true));
    await act(async () => { result.current.saveCfg({ enabled: false }); await flush(); });

    await waitFor(() => expect(relayPut).toHaveBeenCalledTimes(1));
    const body = relayPut.mock.calls[0][1] as Record<string, unknown>;
    const cfg = body.config as Record<string, unknown>;
    expect(cfg).toHaveProperty("enabled");
    expect(cfg.enabled).toBe(false);
  });

  it("carries the server-resolved starter pack through, and never invents one", async () => {
    const rows = [{ kind: "video", label: "Post-gym", description_for_ai: "…",
      media_ids: [], preview_media_ids: [], price_cents: 800, tip_unlock_cents: 800,
      is_free_teaser: false, tags: [], enabled: true }];
    relayGet.mockResolvedValue({ ...CONFIG_RESPONSE, starter_singles: rows });
    const { result } = renderHook(() => useSellerConfig("2024813"), { wrapper });
    await waitFor(() => expect(result.current.starterSingles).toEqual(rows));

    cleanup();
    // A response without the field (an older relay) must yield NOTHING to load —
    // never a client-side guess at which lane's templates to write.
    client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
    const { starter_singles: _drop, ...bare } = { ...CONFIG_RESPONSE, starter_singles: rows };
    relayGet.mockResolvedValue(bare);
    const r2 = renderHook(() => useSellerConfig("123456789"), { wrapper });
    await waitFor(() => expect(r2.result.current.configLoaded).toBe(true));
    expect(r2.result.current.starterSingles).toEqual([]);
  });
});

/**
 * The script-pack editor is the ONE seller surface where wrong-lane copy is more
 * than a typo: it pre-fills each textarea from `pack` and stores the whole box as
 * an override on the first keystroke, and the pack is a no-model-in-the-loop send
 * path. The LINES are laned by the server (`script_packs.shipped_pack`); the COPY
 * around them names roles instead, so it is correct on both lanes with no branch —
 * which is the only version that cannot drift.
 */
describe("ScriptPackCard copy", () => {
  const PACK = { question_hook: ["you around {name}"], haggle_counter: ["{price}. last time i move"] };
  const HELP = { question_hook: "opens a scene", haggle_counter: "the price comes down once" };
  const noop = () => {};

  it("never names the creator's gender", () => {
    const { container } = render(<ScriptPackCard pack={PACK} help={HELP} text={{}}
      setText={noop} onSave={noop} saving={false} saved={false} />);
    expect(container.textContent ?? "").not.toMatch(/\b(she|her|hers|girl)\b/i);
    expect(container.textContent).toContain("The lines");
  });

  it("renders the help the SERVER sent, and nothing when it sent none", () => {
    // The slot schema and its help live in `script_packs`; the card is a renderer.
    // A local table here would be a third enumeration of a server-owned list.
    const { container } = render(<ScriptPackCard pack={PACK} help={HELP} text={{}}
      setText={noop} onSave={noop} saving={false} saved={false} />);
    expect(container.textContent).toContain("the price comes down once");

    cleanup();
    const bare = render(<ScriptPackCard pack={PACK} help={{}} text={{}}
      setText={noop} onSave={noop} saving={false} saved={false} />);
    // A slot with no hint still renders its box — never a crash, never an invented hint.
    expect(bare.container.textContent).toContain("haggle_counter");
  });
});
