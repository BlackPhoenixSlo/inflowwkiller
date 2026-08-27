/**
 * THE LANE FOLLOWS THE DROPDOWN, AND THE SELLER TAB HEARS ABOUT IT.
 *
 * Two behaviours this branch shipped and never tested — reverting either left every
 * suite green:
 *
 *  1. The Creator dropdown is LOCAL FORM STATE until Save. "Reset to defaults" and
 *     the canon field labels therefore have to read the SELECTED lane, not the stored
 *     column — otherwise switching an account to Male and pressing Reset refills it
 *     with the female starter brain, which is the exact flow the male brain exists
 *     for, and the operator fills "Why she started OF" on a male account.
 *
 *  2. `GET /admin/ai-chatter-config` resolves `script_pack` and `starter_singles`
 *     from that same column SERVER-SIDE, but it is a different query key with a 60s
 *     staleTime — and BrainPanel and the seller tabs are mounted on one page. Without
 *     an invalidation, flipping to Male left the seller tab serving the female pack
 *     for the rest of the session, and "Load starter pack" then PERSISTED the female
 *     `description_for_ai` pitch contracts onto a male account.
 */
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from "vitest";
import { act, cleanup, fireEvent, render, renderHook, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

vi.mock("@/lib/relay", async (importOriginal) => {
  const mod = await importOriginal<typeof import("@/lib/relay")>();
  return { ...mod, relay: { ...mod.relay, get: vi.fn(), put: vi.fn() } };
});
// The panel's non-lane dependencies. Everything else is real, because the thing
// under test is BrainPanel's own wiring.
vi.mock("@/hooks/useAccounts", () => ({
  useActiveAccounts: () => [{ id: "ACCOUNT_ID_2", username: "blake" }],
}));
vi.mock("@/components/chat/VaultPicker", () => ({ VaultPicker: () => null }));
vi.mock("@/hooks/useVaultMediaByIds", () => ({ useVaultMediaByIds: () => ({}) }));

import BrainPanel from "@/components/automations/BrainPanel";
import { useAccountConfig, useSaveAccountConfig } from "@/hooks/useAccountConfig";
import { useAiChatterConfig } from "@/hooks/useCatalog";
import { relay } from "@/lib/relay";

const relayGet = relay.get as unknown as Mock;
const relayPut = relay.put as unknown as Mock;

const ACC = "ACCOUNT_ID_2";

let client: QueryClient;
const wrapper = ({ children }: { children: ReactNode }) =>
  <QueryClientProvider client={client}>{children}</QueryClientProvider>;

beforeEach(() => {
  client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } },
  });
  relayGet.mockReset();
  relayPut.mockReset();
  relayPut.mockResolvedValue({ ok: true });
});
afterEach(() => cleanup());

describe("the starter brain follows the SELECTED lane", () => {
  const ACCOUNT_CONFIG = {
    account_id: ACC,
    config: { voice: "her", persona: "", persona_facts: {}, time_activities: {},
              time_images: {}, model_by_purpose: {}, language: "en", location: null,
              timezone: null, utc_offset: 0, daily_cost_cap_cents: 100, model: null },
    defaults: { persona: "You are Ava, flirty and fabulous." },
    defaults_by_voice: {
      her: { persona: "You are Ava, flirty and fabulous." },
      him: { persona: "You are blake, a fighter's build." },
    },
    persona_fact_fields: [
      { key: "why_of", label: "Why she started OF", placeholder: "", operator_only: false }],
    persona_fact_fields_by_voice: {
      her: [{ key: "why_of", label: "Why she started OF", placeholder: "to pay off her studies", operator_only: false }],
      him: [{ key: "why_of", label: "Why he started OF", placeholder: "", operator_only: false }],
    },
    slots: [], model_options: [], purposes: [], languages: [{ code: "en", label: "English" }],
  };

  /** Render the REAL panel. A test that re-implements the panel's rule and asserts
   *  its own copy passes with the panel reverted — which is what the first version of
   *  this file did, and the mutation (BrainPanel reading `config.voice` instead of
   *  `form.voice`) left 442/442 green. Only observable output proves the wiring. */
  const mount = async (body: unknown = ACCOUNT_CONFIG) => {
    relayGet.mockImplementation((path: string) =>
      Promise.resolve(String(path).startsWith("/admin/account-config") ? body : {}));
    render(<QueryClientProvider client={client}><BrainPanel /></QueryClientProvider>);
    await waitFor(() => expect(screen.getByTitle(/Whose voice this account writes in/)).toBeTruthy());
  };
  const creatorSelect = () => screen.getByTitle(/Whose voice this account writes in/);

  it("relabels the canon the moment the dropdown moves, before any Save", async () => {
    await mount();
    expect(screen.getByText("Why she started OF")).toBeTruthy();

    // The dropdown is FORM state — nothing is saved yet. The labels must follow it
    // anyway: they are what the operator types against, so a female label on a male
    // account is an instruction to write female canon.
    await act(async () => { fireEvent.change(creatorSelect(), { target: { value: "him" } }); });
    await waitFor(() => expect(screen.getByText("Why he started OF")).toBeTruthy());
    expect(screen.queryByText("Why she started OF")).toBeNull();
    expect(relayPut).not.toHaveBeenCalled();
  });

  it("Reset fills the SELECTED lane's brain, not the stored column's", async () => {
    await mount();
    await act(async () => { fireEvent.change(creatorSelect(), { target: { value: "him" } }); });
    await act(async () => { fireEvent.click(screen.getByText("Reset to defaults")); });

    // The bug this branch exists to fix: one click turned a male account into a
    // 22-year-old woman with "him" still selected in the dropdown.
    await waitFor(() =>
      expect(screen.getByDisplayValue("You are blake, a fighter's build.")).toBeTruthy());
    expect(screen.queryByDisplayValue("You are Ava, flirty and fabulous.")).toBeNull();
    expect((creatorSelect() as HTMLSelectElement).value).toBe("him");
  });

  /** THE OTHER DIRECTION. Every fixture above is a stored-FEMALE account, so a
   *  panel that ignored the stored column entirely — `const lane = form?.voice ??
   *  "her"` — passed all of them. A stored-MALE account is the case that catches
   *  it, and it is also the case the male lane actually ships in. */
  it("a stored-male account starts in its own lane, before any dropdown move", async () => {
    await mount({ ...ACCOUNT_CONFIG, config: { ...ACCOUNT_CONFIG.config, voice: "him" } });
    expect((creatorSelect() as HTMLSelectElement).value).toBe("him");
    expect(screen.getByText("Why he started OF")).toBeTruthy();
    expect(screen.queryByText("Why she started OF")).toBeNull();
    // …and a blank male account seeds from the MALE starter brain, not Ava.
    await waitFor(() =>
      expect(screen.getByDisplayValue("You are blake, a fighter's build.")).toBeTruthy());
    await act(async () => { fireEvent.click(screen.getByText("Reset to defaults")); });
    await waitFor(() =>
      expect(screen.getByDisplayValue("You are blake, a fighter's build.")).toBeTruthy());
    expect(screen.queryByDisplayValue("You are Ava, flirty and fabulous.")).toBeNull();
  });

  /** 🪄 Enrich REPLACES `persona_facts` wholesale and says "review, then Save".
   *  With no boxes rendered there is nothing to review and Save persists model
   *  output unread — into the block the prompt calls "THESE ARE THE FACTS ABOUT
   *  YOU … you must never say anything that contradicts them". Same rule as
   *  Reset: a control that cannot be answered for is not clickable. */
  it("Enrich is not clickable while the canon slate is empty", async () => {
    await mount({ ...ACCOUNT_CONFIG,
                  config: { ...ACCOUNT_CONFIG.config, persona: "blake, written by hand." },
                  persona_fact_fields_by_voice: {} });
    expect((screen.getByText("🪄 Enrich") as HTMLButtonElement).disabled).toBe(true);
  });
});

describe("saving the lane refreshes what the lane derives", () => {
  it("invalidates the seller payload, not just the brain", async () => {
    relayGet.mockImplementation((path: string) =>
      Promise.resolve(path.startsWith("/admin/account-config")
        ? { account_id: ACC, config: { voice: "her" }, defaults: {}, defaults_by_voice: {},
            persona_fact_fields: [], persona_fact_fields_by_voice: {},
            slots: [], model_options: [], purposes: [], languages: [] }
        : { account_id: ACC, config: {}, defaults: {}, script_pack: {}, slot_help: {},
            starter_singles: [], timezone: null, utc_offset: 0, tz_offset_minutes: null,
            derived_sleep_window: ["02:00", "06:00"], effective_sleep_window: ["02:00", "06:00"],
            default_sleep_window: ["02:00", "06:00"], sleep_source: "default" }));

    // Both mounted on the same page, which is the whole problem.
    const brain = renderHook(() => useAccountConfig(ACC), { wrapper });
    const seller = renderHook(() => useAiChatterConfig(ACC), { wrapper });
    const save = renderHook(() => useSaveAccountConfig(ACC), { wrapper });
    await waitFor(() => expect(seller.result.current.data).toBeTruthy());

    const before = relayGet.mock.calls.filter(
      (c) => String(c[0]).startsWith("/admin/ai-chatter-config")).length;
    expect(before).toBeGreaterThan(0);

    await act(async () => { await save.result.current.mutateAsync({ voice: "him" } as never); });

    // The seller payload is DERIVED from the column that write just moved, so it has
    // to refetch. Without this the tab keeps the female pack and "Load starter pack"
    // persists female pitch contracts onto a male account.
    await waitFor(() => {
      const after = relayGet.mock.calls.filter(
        (c) => String(c[0]).startsWith("/admin/ai-chatter-config")).length;
      expect(after).toBeGreaterThan(before);
    });
    expect(brain.result.current).toBeTruthy();
  });
});
