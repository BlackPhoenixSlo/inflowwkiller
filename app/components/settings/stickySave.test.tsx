/**
 * The pinned Save bar.
 *
 * The automation tabs are tall — AI Upseller's in-flow Save sits ~2,000px down
 * the scroll, PPV Library's ~15,000px with a full catalogue — so each tab grew a
 * SECOND Save pinned to the bottom of the viewport. The in-flow row stays
 * exactly where it was; this is additive.
 *
 * Four properties are worth a test and two of them guard against writing
 * something the operator did not ask for:
 *
 *   • The pin reads the SAME gate as the in-flow button. For the seller tabs
 *     that gate carries `configLoaded`, and `useSellerConfig` in
 *     sellerShared.tsx spells out why: a save on a FAILED load posts a sparse
 *     blob that REPLACES the account's stored config. A pinned button that
 *     skipped that gate would wipe an account's settings with one click from a
 *     convenience control. That is the case pinned hardest below, and it is
 *     asserted on the WIRE — a `disabled` attribute that looks right while a
 *     PUT still goes out is precisely the failure that matters.
 *
 *     It is asserted TWICE, on two tabs, because there are two ways to lose the
 *     gate and only one of them is about the expression. UpsellerTab pins the
 *     expression; NudgeOnlineTab pins the PLACEMENT — its bar lives outside the
 *     `isLoading ? … : <Card>` ternary that holds the form, so it is on screen
 *     at a moment when the in-flow button does not exist at all, and a gate that
 *     only watched the mutations was wide open there.
 *
 *   • That gate covers EVERY store the save writes, and covers each of them for
 *     the right window. NudgeOnlineTab writes two: the config blob and an
 *     automation RULE, and the rule list is a second query with its own
 *     lifetime — with it still in flight `rule` is null, so a save creates a
 *     duplicate detector instead of editing the one already there. The mirror
 *     matters just as much: that list POLLS, so the gate reads `data !==
 *     undefined` and not `isSuccess`, or one failed background refetch would
 *     take both Save controls away mid-edit. Both directions are asserted.
 *
 *   • The ✓ never outlives the state it describes. Pinned to the viewport, a
 *     stale "Saved ✓" is a standing claim about what is on screen.
 *
 *   • It fires the same mutation as the in-flow button, not a copy of the save
 *     logic that can drift.
 *
 * The bar is addressed by `data-testid`, never by role+name: its accessible name
 * is deliberately identical to the in-flow twin's, so a name lookup cannot tell
 * the two apart. `inFlowSave()` / `pinnedSave()` in app/test-utils.tsx say which
 * one a case means.
  *
 * ONE BLOCK IN THIS FILE IS NOT ABOUT THE PIN, and it is exempt from the rule
 * above on purpose: "the texting-style card's load gate" covers
 * `TextingStyleCard`, which renders an in-flow `SaveRow` and no bar at all — so
 * there is no testid to address and no twin to disambiguate from, and it uses
 * role+name. It lives here because it pins a `canSave` gate, which is this
 * file's subject; the pinned bar is not. (Noted because this preamble has
 * already gone stale once by gaining a subject rather than by changing — the
 * same way `loadGateNote`'s JSDoc did.)
*/
import { afterEach, describe, expect, it, vi, type Mock } from "vitest";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

vi.mock("@/lib/relay", async (importOriginal) => {
  const mod = await importOriginal<typeof import("@/lib/relay")>();
  return {
    ...mod,
    relay: { ...mod.relay, get: vi.fn(), put: vi.fn(), post: vi.fn(), patch: vi.fn() },
  };
});

const ACCT = "123456789";

// NudgeOnlineTab picks its own account off the signed-in owner's list rather
// than taking one as a prop, and that list is behind the user/chatter contexts.
// Only the one selector is replaced; everything else in the module is the real
// thing.
vi.mock("@/hooks/useAccounts", async (importOriginal) => {
  const mod = await importOriginal<typeof import("@/hooks/useAccounts")>();
  return {
    ...mod,
    useActiveAccounts: () => [{ id: ACCT, nickname: "acct", has_session: true }],
  };
});

import UpsellerTab from "@/components/settings/UpsellerTab";
import { TextingStyleCard } from "@/components/settings/sellerShared";
import NudgeOnlineTab from "@/components/settings/NudgeOnlineTab";
import WebhookDispatchTab from "@/components/settings/WebhookDispatchTab";
import { AutomationSwitchCard } from "@/components/settings/AutomationSwitch";
import { relay } from "@/lib/relay";
import { inFlowSave, pinnedSave } from "@/test-utils";

const relayGet = relay.get as unknown as Mock;
const relayPut = relay.put as unknown as Mock;
const relayPost = relay.post as unknown as Mock;

const SAVE = /Save Upseller settings/i;

function configResponse(
  config: Record<string, unknown> = {},
  script_pack: Record<string, string[]> = {},
) {
  return {
    account_id: ACCT,
    config: { enabled: true, ...config },
    defaults: {},
    script_pack,
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

/** The same wrapper, but bound to a client the CASE can reach — the only way to
 *  drive a background refetch by hand instead of waiting 30s for the poll. */
function wrapperFor(qc: QueryClient) {
  return function Bound({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
  };
}

const pinned = () => pinnedSave(SAVE);
const inFlow = () => inFlowSave(SAVE);

afterEach(() => { cleanup(); vi.clearAllMocks(); });

describe("the pinned Save bar", () => {
  it("is there on a tab that saves, alongside the in-flow row it duplicates", async () => {
    relayGet.mockImplementation(async (path: string) =>
      path.includes("ai-chatter-config") ? configResponse() : {});
    render(<UpsellerTab accountId={ACCT} />, { wrapper });

    // Both exist. The in-flow row was NOT relocated to make room for the pin.
    await waitFor(() => expect(screen.getByTestId("sticky-save-bar")).toBeTruthy());
    expect(screen.getAllByRole("button", { name: SAVE }).length).toBeGreaterThanOrEqual(2);
    expect(inFlow()).toBeTruthy();
  });

  it("fires the SAME mutation as the in-flow button", async () => {
    relayGet.mockImplementation(async (path: string) =>
      path.includes("ai-chatter-config") ? configResponse({ takeover_enabled: true }) : {});
    relayPut.mockResolvedValue({ ok: true });
    const user = userEvent.setup();
    render(<UpsellerTab accountId={ACCT} />, { wrapper });

    await waitFor(() => expect(pinned().button).not.toBeDisabled());
    await user.click(pinned().button);
    await waitFor(() => expect(relayPut).toHaveBeenCalled());
    const fromPin = relayPut.mock.calls.at(-1);

    relayPut.mockClear();
    await user.click(inFlow());
    await waitFor(() => expect(relayPut).toHaveBeenCalled());
    const fromRow = relayPut.mock.calls.at(-1);

    // Same endpoint, same body — one handler, not two copies of the save logic.
    expect(fromPin![0]).toEqual(fromRow![0]);
    expect(fromPin![1]).toEqual(fromRow![1]);
  });

  it("🚨 stays disabled when the config came back EMPTY, and sends NOTHING", async () => {
    // THE DATA-LOSS GUARD, on the one path where it is actually load-bearing.
    // UpsellerTab early-returns for no-account, still-loading and failed — so
    // the form is only ever on screen once the query settled successfully. What
    // it does NOT early-return on is a 200 with an empty body: `configLoaded` is
    // `isSuccess && !!data` in useSellerConfig, so that renders the whole form
    // with the gate closed and the disabled Save buttons are the only thing
    // left. With nothing to be sparse against a save posts
    // {sleep_window: null, script_pack_overrides: {}} and REPLACES the
    // account's stored blob.
    relayGet.mockImplementation(async (path: string) =>
      path.includes("ai-chatter-config") ? null : {});
    relayPut.mockResolvedValue({ ok: true });
    const user = userEvent.setup();
    render(<UpsellerTab accountId={ACCT} />, { wrapper });

    await waitFor(() => expect(screen.getByTestId("sticky-save-bar")).toBeTruthy());

    // Disabled in lockstep with the in-flow button — the pin does not
    // re-implement the gate, it is the same control rendered twice.
    expect(pinned().button).toBeDisabled();
    expect(inFlow()).toBeDisabled();

    // And the wire is silent. A disabled attribute alone would not prove it:
    // the failure this guards is a button that looks right while a PUT goes out.
    await user.click(pinned().button);
    expect(relayPut).not.toHaveBeenCalled();
  });

  it("is absent entirely when the config load FAILED", async () => {
    // Stronger than disabling: UpsellerTab early-returns a ConfigLoadError and
    // renders no form at all, so there is no pinned bar to mis-click either.
    relayGet.mockImplementation(async (path: string) => {
      if (path.includes("ai-chatter-config")) throw new Error("relay down");
      return {};
    });
    render(<UpsellerTab accountId={ACCT} />, { wrapper });

    await waitFor(() => expect(screen.getByText(/Retry/i)).toBeTruthy());
    expect(screen.queryByTestId("sticky-save-bar")).toBeNull();
    expect(screen.queryAllByRole("button", { name: SAVE })).toEqual([]);
  });

  it("is absent from a surface with no Save of its own", async () => {
    // AutomationSwitchCard writes IMMEDIATELY to a different store — there is no
    // save on it to pin, and a bar promising one would lie.
    // AutomationSwitch.tsx:21-22.
    relayGet.mockImplementation(async (path: string) =>
      path.startsWith("/admin/automation-kinds")
        ? { kinds: [{
            kind: "welcome_chatter_for_info", label: "Get to know fans",
            recurring: true, summary: "", knobs: [], surface: "rules",
            cadence_default_s: 60,
          }] }
        : { rules: [] });
    render(<AutomationSwitchCard accountId={ACCT} kind="welcome_chatter_for_info"
        title="Get to know fans" />, { wrapper });

    await waitFor(() => expect(screen.getByText(/Get to know fans/i)).toBeTruthy());
    expect(screen.queryByTestId("sticky-save-bar")).toBeNull();
  });
});

describe("the pinned Save bar, on a tab whose form is behind a loading branch", () => {
  const CREATE = /Create automation/i;

  /** `/admin/nudge-config` never settles, so `cfgQ.isLoading` stays true and the
   *  tab renders "Loading…" in place of its form for the whole test. */
  function stallTheConfig() {
    relayGet.mockImplementation((path: string) => {
      if (path.startsWith("/admin/nudge-config")) return new Promise(() => {});
      return Promise.resolve({ rules: [] });
    });
  }

  it("🚨 is on screen DURING the fetch, disabled, and sends NOTHING", async () => {
    // THE PLACEMENT CASE. NudgeOnlineTab wraps its whole form in
    // `cfgQ.isLoading ? <div>Loading…</div> : <Card>…</Card>`, and the pinned
    // bar is a SIBLING of that ternary — it has to be, because `sticky` only
    // pins while its own container is on screen and the form's Card is not the
    // container that spans the tab. So the bar is the one Save control that
    // exists while the form does not, and the mutation-only gate it shipped
    // with (`createM/updateM/saveCfgM.isPending`) is all false during a FETCH.
    //
    // One click on that button ran buildConfig() over `form` and `slots` still
    // at their `useState({})` seeds and PUT `{slots:{}}` to /admin/nudge-config
    // — a whole-blob replace, i.e. the account's nudge config gone. The gate
    // now carries the load half, and this asserts it where it broke: on the
    // WIRE, not on the attribute.
    stallTheConfig();
    const user = userEvent.setup();
    render(<NudgeOnlineTab />, { wrapper });

    await waitFor(() => expect(screen.getByTestId("sticky-save-bar")).toBeTruthy());

    // The form really is absent — otherwise this would be testing nothing.
    expect(screen.getByText(/Loading…/i)).toBeTruthy();
    expect(
      screen.getAllByRole("button", { name: CREATE }).filter(
        (b) => !pinnedSave(CREATE).bar.contains(b)),
    ).toEqual([]);

    expect(pinnedSave(CREATE).button).toBeDisabled();

    await user.click(pinnedSave(CREATE).button);
    expect(relayPut).not.toHaveBeenCalled();   // the config blob
    expect(relayPost).not.toHaveBeenCalled();  // the automation rule
  });

  it("wakes up once the config lands, and then both controls agree", async () => {
    // The mirror of the case above: the gate is a LOAD gate, not a permanent
    // "off", and the pin and the in-flow row open together.
    relayGet.mockImplementation(async (path: string) => {
      if (path.startsWith("/admin/nudge-config")) {
        return { account_id: ACCT, config: { enabled: true }, defaults: {} };
      }
      return { rules: [] };
    });
    render(<NudgeOnlineTab />, { wrapper });

    await waitFor(() => expect(pinnedSave(CREATE).button).not.toBeDisabled());
    expect(inFlowSave(CREATE)).not.toBeDisabled();
  });
});

/* ── the OTHER store the same gate has to cover ─────────────────────────── */

describe("the pinned Save bar, against the automation-rule list", () => {
  const CREATE = /Create automation/i;
  const CHANGES = /Save changes/i;

  const NUDGE_CFG = { account_id: ACCT, config: { enabled: true }, defaults: {} };
  const RULE = {
    id: 7, account_id: ACCT, name: "Nudge online", kind: "nudge_online",
    is_enabled: true, every_seconds: 60, trigger: { type: "cadence" },
    payload: {}, quiet_hours: null, created_at: null, last_run: null,
    next_due_at: null, has_pending_job: false,
  };

  /** The config lands; the RULE LIST is the thing still in flight. */
  function stallTheRuleList() {
    relayGet.mockImplementation((path: string) => {
      if (path.startsWith("/admin/nudge-config")) return Promise.resolve(NUDGE_CFG);
      if (path.startsWith("/admin/automation-rules")) return new Promise(() => {});
      return Promise.resolve({});
    });
  }

  it("🚨 is disabled until the RULE LIST lands, and creates no second detector", async () => {
    // The config gate is not the only one this tab needs. save() branches
    // create-vs-update on `rule`, which comes from the rule LIST, and that list
    // is a different query with a different lifetime. With it still in flight
    // `rule` is null, so a click takes the CREATE branch and POSTs a second
    // nudge_online rule beside the one already there —
    // service/automation_rules_api.py enforces no (account_id, kind)
    // uniqueness, so both survive and both run the 60s detector: the fan is
    // nudged twice, and later edits reach only one of the pair.
    stallTheRuleList();
    const user = userEvent.setup();
    render(<NudgeOnlineTab />, { wrapper });

    // The FORM is on screen — so this case is about the rule list, not a
    // second helping of the config gate the cases above already pin.
    await waitFor(() => expect(inFlowSave(CREATE)).toBeTruthy());
    expect(screen.queryByText(/Loading…/i)).toBeNull();

    expect(pinnedSave(CREATE).button).toBeDisabled();
    expect(inFlowSave(CREATE)).toBeDisabled();

    // The wire, not the attribute.
    await user.click(pinnedSave(CREATE).button);
    expect(relayPost).not.toHaveBeenCalled();  // the duplicate rule
    expect(relayPut).not.toHaveBeenCalled();   // the config blob
  });

  it("survives a FAILED background refetch of that list — both controls stay live", async () => {
    // The mirror hazard, and the reason the gate reads `rulesQ.data !== undefined`
    // rather than `rulesQ.isSuccess`. That query polls every 30s and refetches on
    // window focus (hooks/useAutomations.ts), and a failed BACKGROUND refetch
    // moves the observer to status "error" while `data` stays exactly as it was.
    // An `isSuccess` gate reads that as "not loaded" and takes BOTH Save controls
    // away from an operator three screens into an edit — silently, since `error`
    // carries nothing — until some later poll happens to succeed.
    relayGet.mockImplementation(async (path: string) => {
      if (path.startsWith("/admin/nudge-config")) return NUDGE_CFG;
      if (path.startsWith("/admin/automation-rules")) return { rules: [RULE] };
      return {};
    });
    relayPut.mockResolvedValue({ ok: true });
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const user = userEvent.setup();
    render(<NudgeOnlineTab />, { wrapper: wrapperFor(qc) });

    await waitFor(() => expect(pinnedSave(CHANGES).button).not.toBeDisabled());

    // The next poll fails. Nothing else changes.
    relayGet.mockImplementation(async (path: string) => {
      if (path.startsWith("/admin/nudge-config")) return NUDGE_CFG;
      if (path.startsWith("/admin/automation-rules")) throw new Error("relay down");
      return {};
    });
    await act(async () => { await qc.refetchQueries().catch(() => {}); });

    // Prove the state this case is about is really on the board: one query
    // holding BOTH an error and its data. Without this the case could pass on a
    // refetch that never happened.
    const wedged = qc.getQueryCache().getAll()
      .filter((q) => q.state.status === "error" && q.state.data !== undefined);
    expect(wedged).toHaveLength(1);

    expect(pinnedSave(CHANGES).button).not.toBeDisabled();
    expect(inFlowSave(CHANGES)).not.toBeDisabled();

    // And it still saves — the gate is open on the wire, not just in the DOM.
    await user.click(pinnedSave(CHANGES).button);
    await waitFor(() => expect(relayPut).toHaveBeenCalled());
  });
});

/* ── the ✓ must never outlive the state it describes ────────────────────── */

describe("the pinned Save bar's confirmation", () => {
  it("drops the ✓ when a script-pack line is edited", async () => {
    // The pack editors were the one edit seam that did not clear the mutation,
    // logged once as cosmetic. It is not: `packOverrides` is folded into
    // buildSparse in sellerShared.tsx, so pack text IS unsaved work saveCfg()
    // would write — and a "Saved ✓" pinned to the bottom of the viewport over
    // it tells the operator their lines are stored when they are not.
    relayGet.mockImplementation(async (path: string) =>
      path.includes("ai-chatter-config")
        ? configResponse({}, { question_hook: ["you around"] })
        : {});
    relayPut.mockResolvedValue({ ok: true });
    const user = userEvent.setup();
    render(<UpsellerTab accountId={ACCT} />, { wrapper });

    await waitFor(() => expect(pinned().button).not.toBeDisabled());
    await user.click(pinned().button);
    await waitFor(() =>
      expect(screen.getAllByText("Saved ✓").length).toBeGreaterThan(0));

    // One keystroke in a pack box, and the whole tab's ✓ has to go — the pinned
    // bar's included, since it is the same mutation rendered again.
    await user.type(screen.getByPlaceholderText("you around"), "!");
    expect(screen.queryAllByText("Saved ✓")).toEqual([]);
  });
});

/* ── what counts as LOADED, on a tab that renders its form anyway ────────── */

describe("the pinned Save bar's load gate, on a config tab with no ConfigLoadError", () => {
  // UpsellerTab (above) early-returns a ConfigLoadError on a failed fetch, so
  // its gate never has to tell a FAILED load apart from a STALE one. The four
  // config tabs that render their form regardless — Autoreply, Instant reply,
  // Tip reward, PPV Library — do, and they all read the same predicate.
  // WebhookDispatchTab stands in for the four: same `const configLoaded`, the
  // smallest form.
  //
  // The predicate is `!!cfgQ.data`, and BOTH halves of that are load-bearing:
  //
  //   • no `isSuccess &&` conjunct. React Query only ever writes `data` from a
  //     successful fetch, so `isSuccess` adds nothing but false negatives — and
  //     the state it goes false in is routine, not exotic: every save here ends
  //     in `qc.invalidateQueries`, which refetches the mounted observer at once.
  //     One failed refetch and the tab sat un-savable showing "Saved ✓".
  //   • `!!`, not `!== undefined`. A 200 with an empty body arrives as `null`,
  //     which `!== undefined` would call loaded — reopening the whole-blob wipe
  //     the gate exists to stop.
  //
  // One case per half, both asserted on the WIRE.

  const SAVE = /Save instant-reply settings/i;
  const ENABLE = /Disabled \(polls every 30s\)/i;

  const WEBHOOK_CFG = {
    account_id: ACCT,
    // delay_seconds is the tell: 12 is this account's STORED value and 5 is the
    // hardcoded fallback, so the PUT body says which of the two the form held.
    config: { enabled: false, delay_seconds: 12 },
    defaults: { enabled: false, delay_seconds: 5, jitter_seconds: 0 },
  };

  it("🚨 stays live through a FAILED background refetch, and PUTs the operator's edit", async () => {
    relayGet.mockImplementation(async (path: string) =>
      path.startsWith("/admin/webhook-config") ? WEBHOOK_CFG : {});
    relayPut.mockResolvedValue({ ok: true });
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const user = userEvent.setup();
    render(<WebhookDispatchTab accountId={ACCT} />, { wrapper: wrapperFor(qc) });

    await waitFor(() => expect(pinnedSave(SAVE).button).not.toBeDisabled());
    // An unsaved edit, mid-form, exactly where the refetch will catch it.
    await user.click(screen.getByRole("checkbox", { name: ENABLE }));

    // The next background refetch fails. Nothing else changes.
    relayGet.mockImplementation(async (path: string) => {
      if (path.startsWith("/admin/webhook-config")) throw new Error("relay down");
      return {};
    });
    await act(async () => { await qc.refetchQueries().catch(() => {}); });
    // THE FLUSH. The observer notifies on a MACROTASK, so the instant after the
    // refetch resolves the CACHE holds the error while the mounted component
    // can still be rendering the pre-refetch snapshot (`isSuccess: true`).
    // Draining it here means the assertions below read the state this case is
    // named for instead of racing it — two reviews probed this library without
    // the flush and took the stale reading for the real one.
    await act(async () => { await new Promise((r) => setTimeout(r, 50)); });

    // Prove the state is really on the board: the config query holding BOTH an
    // error and its data. Without this the case could pass on a refetch that
    // never happened.
    const wedged = qc.getQueryCache().getAll().filter(
      (q) => q.queryKey[0] === "webhook-config"
        && q.state.status === "error" && q.state.data !== undefined);
    expect(wedged).toHaveLength(1);

    // Save is still live — in both controls, and on the wire.
    expect(pinnedSave(SAVE).button).not.toBeDisabled();
    expect(inFlowSave(SAVE)).not.toBeDisabled();
    await user.click(pinnedSave(SAVE).button);
    await waitFor(() => expect(relayPut).toHaveBeenCalled());

    // And it carried the operator's edit over this account's REAL settings —
    // `enabled` flipped, `delay_seconds` still the stored 12 and not the
    // hardcoded 5. A save that posted the fallbacks would be the wipe.
    const body = relayPut.mock.calls.at(-1)![1] as {
      config: { enabled?: boolean; delay_seconds?: number };
    };
    expect(body.config.enabled).toBe(true);
    expect(body.config.delay_seconds).toBe(12);
  });

  it("🚨 stays disabled on a 200 with an EMPTY body, and sends nothing", async () => {
    // `data === null`, `isSuccess === true`: the form is on screen holding
    // nothing but its hardcoded fallbacks, and one save would PUT those over
    // the account's stored blob. This is the case `!== undefined` would open.
    relayGet.mockImplementation(async (path: string) =>
      path.startsWith("/admin/webhook-config") ? null : {});
    relayPut.mockResolvedValue({ ok: true });
    const user = userEvent.setup();
    render(<WebhookDispatchTab accountId={ACCT} />, { wrapper });

    await waitFor(() => expect(screen.getByTestId("sticky-save-bar")).toBeTruthy());
    // The form really rendered — this tab has no ConfigLoadError early return,
    // so the disabled buttons are the only guard there is.
    expect(screen.getByRole("checkbox", { name: ENABLE })).toBeTruthy();

    expect(pinnedSave(SAVE).button).toBeDisabled();
    expect(inFlowSave(SAVE)).toBeDisabled();

    // The wire, not the attribute.
    await user.click(pinnedSave(SAVE).button);
    expect(relayPut).not.toHaveBeenCalled();
  });
});

describe("the texting-style card's load gate", () => {
  // B-5: the one `canSave` on this branch with no case, and the only one that
  // changes an EXISTING control's enablement. The hazard is a merge, not a
  // replace: `saveStyle` sends all six flags EXPLICITLY, and the relay does
  // `{**stored, **body.config}` keeping every present known key, so a save
  // before the load lands writes the six pre-load seeds over whatever the
  // operator had stored. Two of those seeds are `true`, so it is not even a
  // uniform wipe — it silently flips individual flags.
  const STYLE = /Save style/i;
  const TYPOS = /Typos/i;

  it("🚨 disables Save style until the style config lands, and sends nothing", async () => {
    let release: (v: unknown) => void = () => {};
    const hang = new Promise((res) => { release = res; });
    relayGet.mockImplementation(async (path: string) =>
      path.startsWith("/admin/style-config") ? hang : {});
    relayPut.mockResolvedValue({ ok: true });
    const user = userEvent.setup();
    render(<TextingStyleCard accountId={ACCT} />, { wrapper });

    // The card renders its checkboxes from the first frame — it has no
    // isLoading early return, so the disabled button is the only guard.
    await waitFor(() =>
      expect(screen.getByRole("checkbox", { name: TYPOS })).toBeTruthy());
    expect(screen.getByRole("button", { name: STYLE })).toBeDisabled();

    await user.click(screen.getByRole("button", { name: STYLE }));
    expect(relayPut).not.toHaveBeenCalled();

    // The gate is the LOAD, not a permanently dead control.
    release({
      account_id: ACCT,
      config: { ai_chatter: true, typos_ai_chatter: true },
      defaults: {},
    });
    await waitFor(() =>
      expect(screen.getByRole("button", { name: STYLE })).not.toBeDisabled());
  });

  it("saves the operator's STORED flags once loaded, not the pre-load seeds", async () => {
    // NOT a gate case — this one passes with the gate deleted, because the
    // mocked GET settles before the click. What it pins is the SEEDING path:
    // that `saveStyle` sends what the loaded config said, not the six
    // false/false/false/true/false/true seeds. Break the `{...defaults,
    // ...config}` merge in `useSellerStyle` and this is what fails. It is also
    // the only coverage of that seeding anywhere in the repo.
    relayGet.mockImplementation(async (path: string) =>
      path.startsWith("/admin/style-config")
        ? {
            account_id: ACCT,
            config: {
              ai_chatter: true, typos_ai_chatter: true, nonnative_ai_chatter: true,
              cat_stickers: true, consistency_ai_chatter: true,
              nonnative_spacing_ai_chatter: true,
            },
            defaults: {},
          }
        : {});
    relayPut.mockResolvedValue({ ok: true });
    const user = userEvent.setup();
    render(<TextingStyleCard accountId={ACCT} />, { wrapper });

    await waitFor(() =>
      expect(screen.getByRole("button", { name: STYLE })).not.toBeDisabled());
    await user.click(screen.getByRole("button", { name: STYLE }));

    await waitFor(() => expect(relayPut).toHaveBeenCalled());
    const body = relayPut.mock.calls.at(-1)![1] as {
      config: Record<string, boolean>;
    };
    // Every stored true survived — none was overwritten by a seed.
    expect(body.config.ai_chatter).toBe(true);
    expect(body.config.typos_ai_chatter).toBe(true);
    expect(body.config.nonnative_ai_chatter).toBe(true);
    expect(body.config.consistency_ai_chatter).toBe(true);
  });
});
