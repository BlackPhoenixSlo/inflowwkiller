"use client";

/**
 * The Save control an automation tab renders — twice.
 *
 * `SaveRow` is the row itself: the button, the gate it reads, and the saved ✓ /
 * error feedback beside it. `StickySaveBar` is that SAME row inside a shell
 * pinned to the bottom of the viewport. A tab builds ONE object of save props
 * and hands it to both, so the pinned control and the in-flow one cannot say
 * different things — same handler, same gate, same label, same copy.
 *
 * WHY A SECOND CONTROL AT ALL: the tabs under ReadyMadePanel are tall. The AI
 * Upseller tab's in-flow Save sits ~2,000px down the scroll; PPV Library's is
 * ~15,000px down with a full catalogue. Five hand-rolled sticky twins had grown
 * for that reason — UpsellerTab, VaultAiTab, TipRewardTab, PPVLibraryTab, and
 * BrainPanel's own `md:hidden sticky bottom-0` row on this same /automations
 * page — and every one was scoped to phones, so a desktop still scrolled the
 * whole way. Two of the five are replaced by this component; THREE remain
 * (TipReward's and PPV Library's, which is why those two tabs pass
 * `pin="desktop-only"` rather than stack a second bar on a phone, and
 * BrainPanel's, a different panel with a different save, deliberately not
 * adopted).
 *
 * ADDITIVE, never a relocation. Every tab's in-flow Save row stays where it is;
 * this is a second control wired to the same handler.
 *
 * THE CONTRACT IS THE ONE THIS REPO ALREADY HAD — `sellerShared`'s
 * ScriptPackCard: `onSave` / `saving` / `canSave` / `saved` / `error`, with
 * `disabled` derived inside. One opaque `disabled` boolean instead is how a
 * pinned Save goes live over a form that has not loaded: `disabled={busy}` READS
 * as complete, and nothing on the JSX says a load gate is missing. `canSave` is
 * a named slot, so its absence is visible on sight.
 *
 * Three rules this component exists to enforce:
 *
 *   1. `canSave` is the LOAD gate, and it is a data-loss guard, not a nicety.
 *      Every one of these endpoints takes a whole-blob REPLACE, so saving a
 *      form that was never seeded posts placeholders over the account's real
 *      settings. `useSellerConfig` in sellerShared.tsx spells the seller case
 *      out in full ("nothing to be sparse AGAINST"). Hoist ONE gate per tab and
 *      let both controls read it — a gate written twice is a gate that drifts.
 *   2. It shows the same saved ✓ / error feedback the in-flow row does, and the
 *      same "this will not actually send" warnings, through `children`. A
 *      pinned button that silently succeeds is worse than scrolling for one
 *      that talks, and one that hides the warning is worse still. The feedback
 *      must be the SAVE's own: a bar that turns red because some other button
 *      on the tab failed is narrating someone else's action.
 *   3. THE BAR IS THE LAST CHILD OF THE CONTAINER WHOSE MUTATION IT FIRES, and
 *      never of a container that also holds a DIFFERENT form's Save. `sticky`
 *      only pins while its own containing block is on screen, so the container
 *      is the statement of scope: one level too high and a bar labelled for one
 *      form hovers over another form's editors, promising to save what it
 *      cannot.
 *
 * PLACEMENT — bottom, button LEFT-aligned. The operator asked for "top right or
 * bottom right", but desktop bottom-right is taken: MoneyRail.tsx:842-843 docks
 * `fixed z-40 … bottom-3 right-3` there, and is drag-repositionable on top of
 * that. Top-right would fight the 14-tab strip at ReadyMadePanel.tsx:231, which
 * is `overflow-x-auto` — nothing sticky may live inside that, it would scroll
 * sideways out of reach.
 *
 * Bottom-LEFT is not free either, hence `md:pl-10` in the base.
 * components/assistant/AssistantWidget.tsx:182-183 docks
 * `fixed bottom-3 left-3 z-40 hidden md:grid h-9 w-9` — x from 12 to 48px — and
 * it is `md:`-scoped, i.e. it renders at exactly the widths this bar was
 * un-gated for. A bar that is a direct child of a tab's outer div starts its
 * button at x=40 (the page's `p-6` = 24, plus ReadyMadePanel's Card `p-4` = 16),
 * so the dock covered its left 8px and won on z-index (40 over 20). 40px of left
 * padding puts that button at x=64, clear of the dock, and every more deeply
 * nested bar further right again. The MoneyRail still overlaps the bar's RIGHT
 * end — background, and status text long enough to wrap under it — which is
 * cosmetic and not chased.
 *
 * `sticky`, not `fixed`: /automations is `max-w-shell mx-auto`
 * (app/automations/page.tsx:15), so sticky holds its x-position, keeps the host
 * Card's width, and cannot outlive its own tab the way a fixed element would.
 * z-20 sits under the modals (z-50+), the toaster (z-[60]) and the MoneyRail /
 * Assistant docks (z-40) on purpose: a picker or a dialog opened FROM a tab must
 * cover this, not duck under it.
 */

import type { ReactNode } from "react";
import { Save } from "lucide-react";

import { Button } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";

/** The save-control contract, named slot for slot after `ScriptPackCard` in
 *  sellerShared.tsx so the two read the same. `disabled` is DERIVED from these,
 *  never passed in — see rule 1 above. */
export type SaveControl = {
  onSave: () => void;
  /** The mutation is in flight: the button says "Saving…" and the gate closes. */
  saving: boolean;
  /** False while the config hasn't loaded — saving then REPLACES it with
   *  placeholders. It gets its own slot rather than being folded into a
   *  `disabled` boolean precisely so that a tab which forgot it is obvious. */
  canSave?: boolean;
  label?: string;
  saved?: boolean;
  /** Why the last SAVE was rejected. Without it a failed save looks like
   *  nothing happened, and the operator walks away believing it was stored.
   *  Strictly the mutation's own verdict — the LOAD gate has its own slot
   *  below, because the two are different mechanisms and a single slot meant
   *  one of them got smuggled through `children` with its own colour rule. */
  error?: string | null;
  /** Why Save is off — or, after a failed background refresh, why what is on
   *  screen may be stale even though Save still works. Its own slot so both
   *  answers can be on the bar at once and one render path picks the colour. */
  gateNote?: GateNote | null;
  /** The button size, so the pinned twin matches the in-flow one on the tabs
   *  that use the compact row (`sm`) and on the tabs that don't (`md`). */
  size?: "sm" | "md";
  /** The button's weight. Default `primary` — the tab's own Save. `ghost` is
   *  for a SECOND, smaller writer sitting on the same page as one of those
   *  (TextingStyleCard beside ScriptsTab's config Save): two primaries read as
   *  competing for the same edit. */
  variant?: "primary" | "ghost";
};

/** A closed (or newly untrustworthy) load gate, said out loud.
 *
 *  `failed` is the colour, and it is not cosmetic: a query that is merely
 *  WAITING painted `text-red-500` reports a fault that has not happened. Dim
 *  while pending, red once the fetch actually failed. */
export type GateNote = {
  text: string;
  failed: boolean;
};

/** Why Save is off, for a tab whose form renders from a config query — three
 *  states that need three different answers, and a gate of `!!data` closes on
 *  two of them.
 *
 *  The one that is easy to get wrong is the middle one. A failed BACKGROUND
 *  refetch sets `isError` while `data` stays exactly as it was, so a message
 *  keyed on `isError` alone tells an operator their settings "never loaded"
 *  over a form that is showing their real, loaded settings — and a GATE keyed
 *  on `isSuccess` would additionally kill Save over those same good settings.
 *  It is not a rare state: every one of these tabs' saves calls
 *  `qc.invalidateQueries` in `onSuccess`, so a refetch fires immediately after
 *  each save; a remount past the query's `staleTime` fires another whenever the
 *  operator switches automation tabs and back (60s for the four config tabs,
 *  5s and `refetchOnMount: "always"` for TextingStyleCard's style query); and
 *  `refetchOnReconnect` fires one on every network blip. (What does NOT happen
 *  is a focus refetch — every caller's hook sets `refetchOnWindowFocus: false`,
 *  which is the same fact `RetryLoad`'s JSDoc below states as "these queries do
 *  not poll".) So the gate reads `!!data`: React Query
 *  only ever writes `data` from a successful fetch, so it already means "seeded
 *  from this account's real settings", and it is the whole of what the save
 *  needs to be safe.
 *
 *  It must stay `!!data` and NOT `data !== undefined`: a 200 with an empty body
 *  lands as `null`, which is exactly the un-seeded form the gate exists to
 *  stop. (The nudge tabs' rule-list query is the opposite case and correctly
 *  uses `data !== undefined` — there `[]` is a real, loaded answer.)
 *
 *  The last one is the quiet one: an offline/paused query has `fetchStatus
 *  "paused"`, which makes `isLoading` FALSE, so on the four config tabs it
 *  slips past their `if (cfgQ.isLoading) return …` early return (TextingStyleCard
 *  has no such early return, so it renders there from the first frame) and shows
 *  the form with Save dead and — before this — nothing on screen saying why. It
 *  has not failed, so it is `failed: false` and renders dim.
 *
 *  CALLERS: the four config tabs (Autoreply, WebhookDispatch, TipReward,
 *  PPVLibrary) and TextingStyleCard. Re-check the staleTime and early-return
 *  sentences above when a sixth arrives — this comment has been wrong twice: once
 *  as written (it claimed these queries refetch on focus; none of them do), and
 *  once by gaining a caller rather than by changing. */
export function loadGateNote(q: {
  data: unknown; isError: boolean; isSuccess: boolean;
}): GateNote | null {
  if (q.data) {
    return q.isSuccess ? null : {
      // Deliberately says nothing about whether Save works. It used to claim
      // "Save is off until the next refresh lands" (false once the gate became
      // `!!data`), and then "Save still writes it" — which lands beside a red
      // "Save failed" when the relay is down and reads as a contradiction. The
      // enabled button is the honest signal; this sentence is only about the
      // REFRESH having failed.
      text: "Couldn't refresh these settings — what's on screen is still yours.",
      failed: true,
    };
  }
  if (q.isSuccess || q.isError) {
    // Nothing arrived (a failure, or a 200 with an empty body). Every field
    // below is showing its hardcoded fallback.
    return {
      text: "These settings never loaded — saving would replace them with defaults.",
      failed: true,
    };
  }
  return {
    text: "Still waiting for these settings — Save is off until they arrive.",
    failed: false,
  };
}

/** The one control that can clear a `loadGateNote`. Without it the operator's
 *  only recovery from a failed config fetch is a full page reload — these
 *  queries do not poll. `ConfigLoadError` in sellerShared.tsx is the fuller
 *  answer (no form at all, plus this button); this is that button alone, for
 *  the tabs that render their form anyway and gate the Save instead. */
export function RetryLoad({ q }: {
  q: { isFetching: boolean; refetch: () => unknown };
}) {
  return (
    <Button size="sm" variant="secondary" disabled={q.isFetching}
      onClick={() => void q.refetch()}>
      {q.isFetching ? "Retrying…" : "Retry"}
    </Button>
  );
}

/** One Save control: the button, its derived gate, and its feedback. Rendered
 *  in normal flow by the tab, and again by StickySaveBar inside the pinned
 *  shell — same props object both times. */
export function SaveRow({
  onSave, saving, canSave = true, label = "Save", size = "md",
  variant = "primary", saved, error, gateNote, children, className, testId,
}: SaveControl & {
  /** Warnings the operator must see before saving, and row-local extras
   *  (a "Run now" button, the raw-JSON escape hatch). */
  children?: ReactNode;
  /** Where the row sits. Layout only for most callers — but two hand it a whole
   *  shell: TipRewardTab's in-flow row (`sticky bottom-0 … md:static`) and
   *  PPVLibraryTab's (the `max-md:`-prefixed twin) each pin THEMSELVES below
   *  768px. That is why those two tabs' shared bars take `pin="desktop-only"`:
   *  the phone width already has a pinned control, hand-rolled, and the union
   *  below deliberately does not grow a third value to absorb it (doing so
   *  would mean deleting a desktop in-flow row, which is the one thing this
   *  component promises not to do). */
  className?: string;
  /** The pinned twin carries the same accessible name as the in-flow row on
   *  purpose, so a role+name lookup cannot tell the two apart. A test that
   *  means the PINNED one addresses it through this. */
  testId?: string;
}) {
  return (
    <div
      data-testid={testId}
      className={cn("flex items-center gap-2 flex-wrap", className)}
    >
      <Button size={size} variant={variant} onClick={onSave}
        disabled={saving || !canSave}>
        <Save size={14} />
        {saving ? "Saving…" : label}
      </Button>
      {saved && <span className="text-xs text-emerald-500">Saved ✓</span>}
      {error && <span className="text-xs text-red-500">{error}</span>}
      {gateNote && (
        <span className={cn("text-xs",
          gateNote.failed ? "text-red-500" : "text-fg-dim")}>
          {gateNote.text}
        </span>
      )}
      {children}
    </div>
  );
}

/** How many Save controls a phone gets — the component's policy, not a raw
 *  class string decided afresh at each call site (it was answered three
 *  different ways that way, none of them written down).
 *
 *    • "always"       — the tab's in-flow row never pins, so this is the only
 *                       pinned control and it belongs at every width.
 *    • "desktop-only" — the tab's OWN in-flow row already pins below 768px
 *                       (TipRewardTab, PPVLibraryTab), so a phone would get two
 *                       bars stacked; hand the pin to the width that had none.
 *
 *  `hidden` beats the row's base `flex` through twMerge and `md:flex` survives
 *  as a different variant, so the two policies leave no width with two bars and
 *  none with zero. */
export type PinPolicy = "always" | "desktop-only";

const PIN_DISPLAY: Record<PinPolicy, string> = {
  always: "flex",
  "desktop-only": "hidden md:flex",
};

/** How far the bar bleeds sideways, so its `border-t` spans the full width of
 *  the padded Card it scrolls inside instead of stopping short at both ends.
 *  A closed set rather than a raw class string: the value is a STATEMENT ABOUT
 *  THE HOST, not styling, and as a free-form string nothing stopped a caller
 *  writing `bleed="bg-red-500"` and type-checking.
 *
 *  The predicate is "does my host span that Card's content box?", NOT "does my
 *  immediate parent have a p-*": several bars hang off an unpadded div whose
 *  width is still the Card's, and 4 is right for those. 0 is for a host that is
 *  NARROWER than the Card — the nudge tabs' `max-w-2xl` column, VaultAi's
 *  `max-w-4xl` one. Bleeding there is worse than not: `-mx-4` reaches the
 *  Card's inner edge on the left and stops a long way short of it on the right,
 *  so the border-t runs out mid-Card. With no bleed the bar covers exactly the
 *  column that scrolls under it, which is all there is to cover. */
const BLEED: Record<0 | 4 | 5, string> = {
  0: "px-0",
  4: "-mx-4 px-4",
  5: "-mx-5 px-5",
};

export default function StickySaveBar({
  pin = "always",
  hostPadding = 4,
  children,
  ...control
}: SaveControl & {
  pin?: PinPolicy;
  /** The bleed the host needs — see `BLEED`. 4 and 5 match the Card's `p-*`;
   *  0 is a host narrower than its Card. */
  hostPadding?: 0 | 4 | 5;
  children?: ReactNode;
}) {
  return (
    <SaveRow
      {...control}
      testId="sticky-save-bar"
      className={cn(
        "sticky bottom-0 z-20 py-3 rounded-b-2xl",
        "bg-panel/95 backdrop-blur border-t border-border",
        "pb-[calc(env(safe-area-inset-bottom)+0.75rem)]",
        BLEED[hostPadding],
        // After the bleed, so twMerge keeps it: clears the Assistant dock, see
        // the placement note above.
        "md:pl-10",
        PIN_DISPLAY[pin],
      )}
    >
      {children}
    </SaveRow>
  );
}
