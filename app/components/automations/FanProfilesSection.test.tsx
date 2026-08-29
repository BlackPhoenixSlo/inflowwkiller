/**
 * ONE BUTTON THAT TURNS PROFILES ON — THROUGH THE SERVER'S OWN SWITCH.
 *
 * The behaviours pinned here, each with a silent failure mode:
 *
 *  1. The button asks the server to switch each KIND on. It decides nothing —
 *     no create-vs-wake, no cadence, no rule name, no payload. Those live in the
 *     server, and a client copy would drift from it.
 *  2. One failing half must not cancel the other, and the message must name
 *     which half failed.
 *  3. Nothing is claimed or written while the rules are unread or unreadable —
 *     states that look exactly like "this account has no rules" — while a poll
 *     that fails AFTER a good read leaves the label standing and only parks the
 *     button.
 *  4. The section keys itself per account, so a switch mid-write cannot carry
 *     the second write onto the account just moved to. Rendered here WITHOUT a
 *     key on purpose: a harness that supplied one would be testing itself.
 *
 * What a NEW row is furnished with (the catalogued cadence and name, and the
 * OnlyFans push that `_CREATE_PAYLOAD` gives apply_profiles — which is NOT what
 * the editor's form produces) is the server's half of the contract, pinned in
 * service/tests/test_automation_rules_api.py::case_switch_create_uses_catalog_defaults.
 */
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from "vitest";
import { act, cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import type { QueryClient } from "@tanstack/react-query";

vi.mock("@/lib/relay", async (importOriginal) => {
  const mod = await importOriginal<typeof import("@/lib/relay")>();
  // Every verb, not just the ones this component reaches: the spread hands out
  // the REAL method for anything omitted, so an allowlist lets a later edit
  // escape the mock and talk to a live relay instead of failing loudly.
  return {
    ...mod,
    relay: {
      ...mod.relay,
      get: vi.fn(), put: vi.fn(), post: vi.fn(), patch: vi.fn(),
      delete: vi.fn(), uploadFile: vi.fn(),
    },
  };
});

import FanProfilesSection from "@/components/automations/FanProfilesSection";
import { relay } from "@/lib/relay";
import { makeTestQueryClient, renderWithProviders } from "@/test-utils";

const relayGet = relay.get as unknown as Mock;
const relayPost = relay.post as unknown as Mock;
const relayPatch = relay.patch as unknown as Mock;

const ACC = "ACCOUNT_ID_2";

let client: QueryClient;
beforeEach(() => {
  client = makeTestQueryClient();
  relayGet.mockReset();
  relayPost.mockReset();
  relayPatch.mockReset();
  relayPost.mockResolvedValue({ action: "created" });
  relayPatch.mockResolvedValue({});
});
afterEach(() => cleanup());

describe("Fan profiles: one button, and the server owns the policy", () => {
  const rule = (id: number, kind: string, is_enabled: boolean, account = ACC) => ({
    id, account_id: account, name: kind, kind, is_enabled, every_seconds: 86400,
    trigger: { every_seconds: 86400 }, payload: {}, quiet_hours: null,
    created_at: null, last_run: null, next_due_at: null, has_pending_job: false,
  });

  /** Rendered exactly as any caller would — no key. The component keys its own
   *  body internally, and that is the point: were the guarantee the caller's,
   *  this harness supplying it would be the only thing under test. */
  const show = (accountId: string | null) => <FanProfilesSection accountId={accountId} />;

  /** Rules are handed out as a COPY on purpose: returning the array itself would
   *  let a test's push land straight in React Query's cache, so the retry case
   *  would pass with the mutation's `invalidateQueries` deleted — proving nothing
   *  about the refetch it exists to prove. */
  const mount = async (rules: unknown[], accountId: string = ACC) => {
    relayGet.mockImplementation((path: string) =>
      String(path).startsWith("/admin/automation-rules")
        ? Promise.resolve({ rules: rules.slice() })
        : Promise.resolve({}));
    const view = renderWithProviders(show(accountId), client);
    await screen.findByText("Fan profiles");
    return view;
  };
  const press = async (label: RegExp) => {
    const btn = await screen.findByText(label);
    await waitFor(() => expect((btn as HTMLButtonElement).disabled).toBe(false));
    await act(async () => { fireEvent.click(btn); });
  };
  const switchCalls = () =>
    relayPost.mock.calls.filter((c) => String(c[0]) === "/admin/automation-rules/switch");
  /** How many times the rules list was actually fetched — the only way to tell a
   *  real refetch from the mount's own first read. */
  const rulesGets = () =>
    relayGet.mock.calls.filter((c) => String(c[0]).startsWith("/admin/automation-rules?")).length;

  it("switches both kinds on, and sends no cadence and no name of its own", async () => {
    await mount([]);
    await press(/Turn on fan profiles/);
    await waitFor(() => expect(switchCalls().length).toBe(2));

    // toEqual, not toMatchObject: the ABSENCE of every_seconds, name and payload
    // is the point. A body carrying any of them is the client taking back policy
    // the server owns, which is the drift this whole shape exists to prevent.
    expect(switchCalls()[0][1]).toEqual({ account_id: ACC, kind: "gen_info", enable: true });
    expect(switchCalls()[1][1]).toEqual({ account_id: ACC, kind: "apply_profiles", enable: true });
    // Nothing goes to the create/patch routes any more — the policy is server-side.
    expect(relayPatch).not.toHaveBeenCalled();
    expect(relayPost.mock.calls.every((c) => String(c[0]).endsWith("/switch"))).toBe(true);
    // Success carries no message of its own: the on-state line is the signal,
    // and it follows the rules rather than remembering that a button was pressed.
    expect(screen.queryByText(/failed/)).toBeNull();
    expect(screen.queryByText(/Fan profiles are on\./)).toBeNull();
  });

  it("shows the on-state and refuses to fire again once both kinds are running", async () => {
    await mount([rule(11, "gen_info", true), rule(22, "apply_profiles", true)]);
    const btn = await screen.findByText("Fan profiles are on");
    expect((btn as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText("On — profiles are being built and applied.")).toBeTruthy();
    expect(relayPost).not.toHaveBeenCalled();
  });

  /** A parked row beside a running one is a real state (the rules editor can
   *  make it). "On" has to mean the kind has a RUNNING row — the same row the
   *  server would wake — or the label and the button disagree. */
  it("reads the RUNNING row, not the first one", async () => {
    await mount([
      rule(9, "gen_info", false), rule(40, "gen_info", true),
      rule(22, "apply_profiles", true),
    ]);
    const btn = await screen.findByText("Fan profiles are on");
    expect((btn as HTMLButtonElement).disabled).toBe(true);
    expect(relayPost).not.toHaveBeenCalled();
  });

  /** A pending read and an account that owns no rules look identical from the
   *  DOM, so nothing may be claimed or written until THIS account's rows arrive.
   *  A is seeded fully ON to make the failure visible: anything reading "the rows
   *  I have" rather than "the rows for this account" renders A's state under B. */
  it("claims nothing and writes nothing until this account's rules arrive", async () => {
    const A = "111", B = "222";
    // A is fully ON. That is what makes this the real test: with the readiness
    // gate removed, account B renders A's "Fan profiles are on" — a claim about
    // the wrong account — and a fixture where A were merely parked would not
    // notice, because `isOn` would be false either way.
    relayGet.mockImplementation((path: string) => {
      const p = String(path);
      if (p.includes(`account_id=${A}`)) {
        return Promise.resolve({ rules: [rule(11, "gen_info", true), rule(22, "apply_profiles", true)] });
      }
      return new Promise(() => {});   // B never resolves: hold the window open
    });
    const view = renderWithProviders(show(A), client);
    await waitFor(() => expect(screen.getByText("Fan profiles are on")).toBeTruthy());

    view.rerender(show(B));
    // A's state must not be spoken about B — neither on the button nor the line
    // under it — and nothing may be written until B's own rows arrive.
    await waitFor(() =>
      expect((screen.getByText("Turn on fan profiles") as HTMLButtonElement).disabled).toBe(true));
    expect(screen.queryByText("On — profiles are being built and applied.")).toBeNull();
    expect(relayPost).not.toHaveBeenCalled();
  });

  /** The write path's version of the wrong-account bug the readiness gate fixed
   *  on the label path — and the more expensive one, because it turns an engine
   *  ON. The mutation binds its account when it is CALLED, not when the loop
   *  started, so an operator switching accounts during the first round-trip
   *  would otherwise have the second write land on the account they moved to. */
  it("does not finish its writes on an account the operator switched away to", async () => {
    const A = "111", B = "222";
    relayGet.mockImplementation((path: string) =>
      String(path).startsWith("/admin/automation-rules")
        ? Promise.resolve({ rules: [] })
        : Promise.resolve({}));
    // Hold the FIRST switch open so the account can change mid-flight.
    let releaseFirst: (v: unknown) => void = () => {};
    relayPost.mockImplementationOnce(
      () => new Promise((res) => { releaseFirst = res; }));

    const view = renderWithProviders(show(A), client);
    await press(/Turn on fan profiles/);
    await waitFor(() => expect(switchCalls().length).toBe(1));
    expect((switchCalls()[0][1] as { account_id: string }).account_id).toBe(A);

    view.rerender(show(B));
    await act(async () => { releaseFirst({ action: "created" }); });

    // Whatever else happens, nothing may be written to B and nothing may claim
    // anything under it. The instance that fired is gone, so even its failure
    // message cannot land on the account now on screen.
    // Both writes must have been attempted before `every` means anything: over a
    // one-element array it is vacuously true and would pass on the bug.
    await waitFor(() => expect(switchCalls().length).toBe(2));
    expect(switchCalls().every((c) => (c[1] as { account_id: string }).account_id === A)).toBe(true);
    expect(screen.queryByText(/failed/)).toBeNull();
    expect(screen.queryByText("Fan profiles are on")).toBeNull();
  });

  /** The list polls every 30s, and a poll failing AFTER a good read must change
   *  nothing an operator can see or do. query-core sets `status: "error"` on a
   *  background failure while KEEPING the rows, so anything keyed on the query
   *  being happy — rather than on there being rows — parks the button for 30s
   *  and prints "can't say whether profiles are on" directly beneath "On —
   *  profiles are being built and applied."
   *
   *  The fixture is deliberately HALF on: with both kinds running the button is
   *  disabled by `isOn` anyway, and a parked button would be indistinguishable
   *  from a correct one. This asserts the button stays CLICKABLE. */
  it("a failed poll after a good read leaves the section usable and honest", async () => {
    await mount([rule(11, "gen_info", true), rule(22, "apply_profiles", false)]);
    const btn = await screen.findByText("Turn on fan profiles");
    await waitFor(() => expect((btn as HTMLButtonElement).disabled).toBe(false));

    relayGet.mockImplementation((path: string) =>
      String(path).startsWith("/admin/automation-rules")
        ? Promise.reject(new Error("poll failed"))
        : Promise.resolve({}));
    // The refetch rejects — that is the condition under test — and the error
    // state lands a tick later, so wait for it rather than reading straight
    // after `act`, which is what hid this the first time.
    await act(async () => { await client.refetchQueries().catch(() => {}); });
    await waitFor(() => expect(client.getQueryState(
      ["automation-rules", ACC])?.status).toBe("error"));

    // No contradiction under the label, and the button still writes.
    expect(screen.queryByText(/can.t say whether profiles are on/i)).toBeNull();
    await press(/Turn on fan profiles/);
    await waitFor(() => expect(switchCalls().length).toBe(2));
  });

  /** A failed list read is not an empty account, and must not look like one. */
  it("stays disabled, and says so, when the rules list fails to load", async () => {
    relayGet.mockImplementation((path: string) =>
      String(path).startsWith("/admin/automation-rules")
        ? Promise.reject(new Error("relay down"))
        : Promise.resolve({}));
    renderWithProviders(show(ACC), client);
    const btn = await screen.findByText("Turn on fan profiles");
    await waitFor(() => expect((btn as HTMLButtonElement).disabled).toBe(true));
    // No rows ever arrived, so this is the copy that admits knowing nothing.
    expect(screen.getByText(/can.t say whether profiles are on/i)).toBeTruthy();
    expect(relayPost).not.toHaveBeenCalled();
  });

  /** With both writes under ONE try/catch the suite still passes when it is the
   *  SECOND half that fails — the first has already run. Only a failure in the
   *  FIRST half shows whether the second is still attempted. */
  it("a failure in the FIRST half does not skip the second", async () => {
    await mount([]);
    relayPost.mockImplementationOnce(async () => { throw new Error("nope"); });
    relayPost.mockImplementationOnce(async () => ({ action: "created" }));

    await press(/Turn on fan profiles/);
    await waitFor(() => expect(switchCalls().length).toBe(2));

    expect((switchCalls()[0][1] as { kind: string }).kind).toBe("gen_info");
    expect((switchCalls()[1][1] as { kind: string }).kind).toBe("apply_profiles");
    const msg = await screen.findByText(/profile building failed/);
    expect(msg.textContent).toContain("nope");
    expect(msg.textContent).not.toContain("applying failed");
  });

  it("when the second half fails the message says which, and a retry repairs it", async () => {
    const rules: unknown[] = [];
    await mount(rules);
    relayPost.mockImplementationOnce(async () => {
      rules.push(rule(11, "gen_info", true));
      return { action: "created" };
    });
    relayPost.mockImplementationOnce(async () => { throw new Error("boom"); });

    const getsBeforePress = rulesGets();
    await press(/Turn on fan profiles/);
    await waitFor(() => expect(switchCalls().length).toBe(2));
    const msg = await screen.findByText(/applying failed/);
    expect(msg.textContent).toContain("boom");

    // The panel only learns gen_info exists because the switch INVALIDATED the
    // rules query. Counting the GETs is what proves a refetch actually happened.
    await waitFor(() => expect(rulesGets()).toBeGreaterThan(getsBeforePress));
    await waitFor(() => expect(screen.getByText("Turn on fan profiles")).toBeTruthy());

    relayPost.mockResolvedValue({ action: "created" });
    await press(/Turn on fan profiles/);
    await waitFor(() => expect(switchCalls().length).toBe(4));
    expect((switchCalls()[3][1] as { kind: string }).kind).toBe("apply_profiles");
    // The failure line is cleared by the retry rather than left standing.
    await waitFor(() => expect(screen.queryByText(/applying failed/)).toBeNull());
  });
});
