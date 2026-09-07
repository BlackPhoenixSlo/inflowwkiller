/**
 * WHAT THE AUTO-FOLLOW TAB ACTUALLY SAVES — the pool, and everything it must not
 * destroy on the way there.
 *
 * Three defects this pins, all of them silent and all of them found on a rule an
 * operator had already configured correctly:
 *
 *  1. THE POOL. The Target dropdown wrote its `targets` through an if/else chain
 *     whose final `else` was unconditional, so any source the chain had not been
 *     taught fell through and saved `recent_active` — a wrong-pool rule that
 *     compiled, tested green and looked right on screen. The chain is now one
 *     `Record<Source, …>`; this asserts every entry of it, so a new source cannot
 *     be added to the dropdown without a payload to go with it.
 *
 *  2. THE REST OF THE PAYLOAD. PATCH replaces the payload wholesale and this form
 *     holds four of its keys, so a save from here reverted `money_gate: false` to
 *     the gated default and deleted a hand-set `targets.fan_ids`. It must spread.
 *
 *  3. THE INFERRED SOURCE. `targets.source` absent means `all_stored` to the
 *     engine (auto_follow._source_list) and used to mean "expired" to this panel:
 *     the tab misreported a live 3663-fan backfill as OF's short win-back list,
 *     and the next save of any unrelated field wrote that lie into the rule.
 */
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

vi.mock("@/lib/relay", async (importOriginal) => {
  const mod = await importOriginal<typeof import("@/lib/relay")>();
  // The spread hands out the REAL method for anything not stubbed, so an edit
  // that reaches a new verb fails loudly instead of talking to a live relay.
  return {
    ...mod,
    relay: {
      ...mod.relay,
      get: vi.fn(), put: vi.fn(), post: vi.fn(), patch: vi.fn(),
      delete: vi.fn(), uploadFile: vi.fn(),
    },
  };
});

import AutoFollowTab from "@/components/growth/AutoFollowTab";
import { relay } from "@/lib/relay";

const relayGet = relay.get as unknown as Mock;
const relayPost = relay.post as unknown as Mock;
const relayPatch = relay.patch as unknown as Mock;

const ACC = "2024813";
const RULE_ID = 91;

let client: QueryClient;

beforeEach(() => {
  client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } },
  });
  relayGet.mockReset(); relayPost.mockReset(); relayPatch.mockReset();
  relayPost.mockResolvedValue({ id: RULE_ID, kind: "auto_follow" });
  relayPatch.mockResolvedValue({ id: RULE_ID, kind: "auto_follow" });
});
afterEach(() => cleanup());

/** Mount the REAL tab over a rules list of exactly `rules`. */
async function mount(rules: unknown[]) {
  relayGet.mockImplementation((path: string) => {
    const p = String(path);
    if (p.startsWith("/admin/automation-rules")) return Promise.resolve({ rules });
    if (p.startsWith("/admin/smart-lists")) {
      return Promise.resolve({ lists: [{ id: 5, account_id: ACC, name: "Whales", rules: { match: "all", rules: [] }, created_at: null, updated_at: null }] });
    }
    return Promise.resolve({});
  });
  render(<QueryClientProvider client={client}><AutoFollowTab accountId={ACC} /></QueryClientProvider>);
  // Wait for the button the LOADED state shows — "Create automation" is also the
  // pre-fetch state, so accepting either would assert against an empty form.
  const settled = rules.length ? "Save changes" : "Create automation";
  await waitFor(() => expect(screen.getByText(settled)).toBeTruthy());
}

const rule = (payload: Record<string, unknown>) => ({
  id: RULE_ID, account_id: ACC, kind: "auto_follow", name: "Auto-follow / Auto-like",
  every_seconds: 14400, is_enabled: true, payload, last_run: null,
});

const targetSelect = () =>
  screen.getByText("Target").closest("label")!.querySelector("select") as HTMLSelectElement;
const actionSelect = () =>
  screen.getByText("Action").closest("label")!.querySelector("select") as HTMLSelectElement;

async function clickSave() {
  const btn = screen.queryByText("Save changes") ?? screen.getByText("Create automation");
  await act(async () => { fireEvent.click(btn); });
}

/** The payload the last PATCH/POST actually carried. */
async function savedPayload(): Promise<Record<string, unknown>> {
  await waitFor(() => expect(relayPatch.mock.calls.length + relayPost.mock.calls.length).toBeGreaterThan(0));
  const call = relayPatch.mock.calls.at(-1) ?? relayPost.mock.calls.at(-1)!;
  return (call[1] as { payload: Record<string, unknown> }).payload;
}

describe("auto-follow Target → the targets it saves", () => {
  // ONE case per entry in SOURCE_SPECS. A source added to the dropdown with no
  // payload mapping used to save `recent_active` from the chain's final `else`;
  // now the compiler demands the entry and this demands it be the right one.
  const cases: Array<[string, string, Record<string, unknown>]> = [
    ["Recently-expired fans (win-back)", "expired", { source: "expired" }],
    ["Recently-active fans", "recent_active", { source: "recent_active", days: 7 }],
    ["Every fan on file (backfill)", "all_stored", { source: "all_stored" }],
  ];
  for (const [label, value, expected] of cases) {
    it(`saves ${JSON.stringify(expected)} for "${label}"`, async () => {
      await mount([rule({ action: "follow", dry_run: true, targets: { source: "expired" } })]);
      await act(async () => { fireEvent.change(targetSelect(), { target: { value } }); });
      await clickSave();
      expect((await savedPayload()).targets).toEqual(expected);
    });
  }

  it("covers EVERY option the dropdown offers", async () => {
    // S-N10. The list above is hand-written, so "one case per SOURCE_SPECS
    // entry" was a claim nothing checked: a fifth source could join the dropdown
    // (and start saving a payload) with no case here and this file still green.
    // The dropdown is rendered from SOURCE_SPECS, so ask it.
    await mount([rule({ action: "follow", dry_run: true, targets: { source: "expired" } })]);
    const offered = Array.from(targetSelect().options).map((o) => o.value).sort();
    // smart_list has its own case below — it needs a second control set first.
    const covered = [...cases.map(([, v]) => v), "smart_list"].sort();
    expect(offered).toEqual(covered);
  });

  it("saves the picked Smart List, not the pool it came from", async () => {
    await mount([rule({ action: "follow", dry_run: true, targets: { source: "expired" } })]);
    await act(async () => { fireEvent.change(targetSelect(), { target: { value: "smart_list" } }); });
    const listSel = screen.getByText("Smart List").closest("label")!.querySelector("select")!;
    await act(async () => { fireEvent.change(listSel, { target: { value: "5" } }); });
    await clickSave();
    expect((await savedPayload()).targets).toEqual({ source: "smart_list", smart_list_id: 5 });
  });

  it("REPLACES targets wholesale on a source change, leaving no stale key behind", async () => {
    // The other half of the spread fix: merging `targets` would carry
    // `smart_list_id` into an `expired` rule, and the engine would resolve a pool
    // the dropdown is not showing.
    await mount([rule({
      action: "follow", dry_run: true,
      targets: { source: "smart_list", smart_list_id: 5, days: 30 },
    })]);
    await act(async () => { fireEvent.change(targetSelect(), { target: { value: "expired" } }); });
    await clickSave();
    expect((await savedPayload()).targets).toEqual({ source: "expired" });
  });
});

describe("auto-follow save — what it must NOT destroy", () => {
  it("keeps every payload key this form does not own", async () => {
    // ⚠️ MONEY. PATCH is a wholesale replace, so the four form fields used to be
    // the WHOLE new payload: `money_gate: false` reverted to the gated default
    // and a hand-set `targets.fan_ids` vanished, silently, on a save of the cap.
    await mount([rule({
      action: "follow", dry_run: false, daily_cap: 30,
      money_gate: false, min_days_between_pings: 3,
      targets: { source: "expired" },
      some_future_knob: "keep me",
    })]);
    await clickSave();
    const p = await savedPayload();
    expect(p.money_gate).toBe(false);
    expect(p.some_future_knob).toBe("keep me");
  });

  it("leaves targets alone entirely when the operator never touched the pool", async () => {
    // The C3 half. A rule with a hand-set fan_ids pool, or none at all, must
    // survive a save of some unrelated field. Editing "Max actions / run" is not
    // a decision about who to follow.
    await mount([rule({
      action: "follow", dry_run: false, daily_cap: 30,
      targets: { source: "fan_ids", fan_ids: [11, 22, 33] },
    })]);
    const cap = screen.getByText("Max actions / run").closest("label")!.querySelector("input")!;
    await act(async () => { fireEvent.change(cap, { target: { value: "12" } }); });
    await clickSave();
    const p = await savedPayload();
    expect(p.targets).toEqual({ source: "fan_ids", fan_ids: [11, 22, 33] });
    expect(p.daily_cap).toBe(12);
  });

  it("does not invent a targets key for a rule that has none", async () => {
    // `{"action":"follow","dry_run":false}` IS the engine's all_stored backfill.
    // Writing `targets:{source:"expired"}` onto it — which the old panel did on
    // any save — swapped a 3663-fan backfill for OF's short win-back list.
    await mount([rule({ action: "follow", dry_run: false, daily_cap: 30 })]);
    await clickSave();
    expect(await savedPayload()).not.toHaveProperty("targets");
  });
});

describe("auto-follow Target → what an EXISTING rule displays", () => {
  it("shows the engine's real default when the rule names no source", async () => {
    // auto_follow._source_list({}) -> ["all_stored"]. Anything else here is the
    // panel reporting a run that is not the one happening.
    await mount([rule({ action: "follow", dry_run: false })]);
    expect(targetSelect().value).toBe("all_stored");
  });

  it("snaps an UNRECOGNISED source to the narrow pool, never to the backfill", async () => {
    // "fan_ids" was dropped from the UI but the engine still honours it for API
    // callers. A value this panel cannot render must fall to the action's first
    // listed pool — widening it to "every fan on file" would be the panel
    // proposing the largest possible money-spending run off a parse failure.
    await mount([rule({ action: "follow", dry_run: false, targets: { source: "fan_ids", fan_ids: [1] } })]);
    expect(targetSelect().value).toBe("expired");
  });

  it("keeps a source the rule does name", async () => {
    await mount([rule({ action: "follow", dry_run: false, targets: { source: "all_stored" } })]);
    expect(targetSelect().value).toBe("all_stored");
  });
});

describe("auto-follow — the shared cooldown ledger", () => {
  it("shows and writes min_days_between_pings for FOLLOW, not just ping", async () => {
    // One key, two actions: `_run_follow` reads it as the backfill's progress
    // window and `_run_ping` as the ping cooldown. Rendered only for ping, a
    // follow rule could never see the knob that paces it — and a save from this
    // tab reset it to 14 behind the operator's back.
    await mount([rule({
      action: "follow", dry_run: false, daily_cap: 30,
      min_days_between_pings: 3, targets: { source: "all_stored" },
    })]);
    const gap = screen.getByText("Min days between actions (per fan)")
      .closest("label")!.querySelector("input") as HTMLInputElement;
    expect(gap.value).toBe("3");
    await clickSave();
    expect((await savedPayload()).min_days_between_pings).toBe(3);
  });

  it("does not write the ledger for an action that does not read it", async () => {
    await mount([rule({ action: "like_messages", dry_run: true, targets: { source: "recent_active", days: 7 } })]);
    await clickSave();
    expect(await savedPayload()).not.toHaveProperty("min_days_between_pings");
  });

  it("switching action rewrites targets for the new pool", async () => {
    await mount([rule({ action: "like_messages", dry_run: true, targets: { source: "smart_list", smart_list_id: 5 } })]);
    await act(async () => { fireEvent.change(actionSelect(), { target: { value: "follow" } }); });
    await act(async () => { fireEvent.change(targetSelect(), { target: { value: "all_stored" } }); });
    await clickSave();
    const p = await savedPayload();
    expect(p.action).toBe("follow");
    expect(p.targets).toEqual({ source: "all_stored" });
  });
});

describe("auto-follow — the panel admitting it cannot describe the rule", () => {
  // R2-7. `sourceFor` snaps an unrenderable source to the action's NARROW pool,
  // which is the right safety answer and the wrong display answer. Since the
  // fix that stopped an untouched save rewriting `targets`, that display is
  // permanent: the rule keeps following its hand-named list forever while the
  // dropdown says something else. auto_follow's own header calls
  // `money_gate:false` + `fan_ids` a RECURRING CHARGE, so it has to be visible.
  const snapNote = () => screen.queryByText(/no control for/);

  it("says so when the stored source has no control", async () => {
    await mount([rule({
      action: "follow", dry_run: false, targets: { source: "fan_ids", fan_ids: [1, 2] },
    })]);
    expect(targetSelect().value).toBe("expired");        // the safe snap stands
    expect(snapNote()).toBeTruthy();
    expect(screen.getByText("fan_ids")).toBeTruthy();
  });

  it("says so for a STACKED source list, which the engine allows and this cannot show", async () => {
    await mount([rule({
      action: "follow", dry_run: false, targets: { source: ["expired", "all_stored"] },
    })]);
    expect(snapNote()).toBeTruthy();
    expect(screen.getByText("expired + all_stored")).toBeTruthy();
  });

  it("stays silent for an ABSENT source, where screen and engine already agree", async () => {
    // Absent is not a snap: the panel shows the engine's own default for it.
    await mount([rule({ action: "follow", dry_run: false })]);
    expect(targetSelect().value).toBe("all_stored");
    expect(snapNote()).toBeNull();
  });

  it("stays silent once the operator picks a pool", async () => {
    // From then on the dropdown IS the truth — the next save writes it.
    await mount([rule({
      action: "follow", dry_run: false, targets: { source: "fan_ids", fan_ids: [1] },
    })]);
    expect(snapNote()).toBeTruthy();
    await act(async () => { fireEvent.change(targetSelect(), { target: { value: "all_stored" } }); });
    expect(snapNote()).toBeNull();
  });
});

describe("auto-follow — the backfill drain hint", () => {
  // R2-8. The old hint asserted "≈3,600-fan audience" to every account (one
  // production account's count, written twice) and modelled the walk at
  // `daily_cap` fans/tick. `daily_cap` bounds NOTIFICATIONS; `_headroom(cap) =
  // cap × 5` bounds the walk, and a skipped fan is stamped too — so the real
  // answer is a range, and the old one was up to 5× too slow.
  const backfill = (extra: Record<string, unknown> = {}) => ({
    ...rule({ action: "follow", dry_run: true, daily_cap: 30, targets: { source: "all_stored" } }),
    ...extra,
  });

  it("invents no audience size when the rule has never run", async () => {
    await mount([backfill()]);
    expect(screen.queryByText(/3,600/)).toBeNull();
    expect(screen.getByText(/how many fans are left to walk/)).toBeTruthy();
  });

  it("uses the engine's MEASURED eligible count and renders a range", async () => {
    // 300 fans, cap 30, every 4h (6 runs/day): the cap binds at 180/day → 2
    // days; the pool binds at 900/day → 1 day. Measured on the engine: 3 ticks
    // all-already-following vs 11 all-free, on a hint that predicted 10 for both.
    await mount([backfill({
      last_run: { status: "ok", started_at: null, stats: { eligible: 300 } },
    })]);
    expect(screen.getByText("300")).toBeTruthy();
    expect(screen.getByText(/1–2 days/)).toBeTruthy();
  });

  it("says never, not a number, when the operator has pressed the stop switch", async () => {
    await mount([backfill({
      payload: { action: "follow", dry_run: true, daily_cap: 0, targets: { source: "all_stored" } },
      last_run: { status: "ok", started_at: null, stats: { eligible: 300 } },
    })]);
    expect(screen.getByText(/never \(max actions is 0\)/)).toBeTruthy();
  });
});
