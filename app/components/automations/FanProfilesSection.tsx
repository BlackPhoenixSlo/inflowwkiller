"use client";

/**
 * FanProfilesSection — the Brain's one-button switch for the fan-profile pair:
 * `gen_info` (read a fan's chat, write a profile) and `apply_profiles` (put the
 * generated nickname + fact-sheet note on the fan, and on OnlyFans). Extracted
 * from BrainPanel for the same reason AudienceSection was: self-contained, and
 * that panel is already well past its healthy size.
 *
 * The pair is one feature to an operator — a profile nobody applies changes
 * nothing on a fan — so it is one button, and this section deliberately owns no
 * other knob. Per-account tuning (cadences, sweep limits, the OnlyFans push)
 * stays in the Automation rules list, which is the surface that owns it.
 *
 * Turning a kind on is `useSwitchKind`, never a create/patch decision made here.
 * That hook exists because the client used to make it and got it wrong; the
 * server picks the RUNNING row rather than the first one, and names, schedules
 * and pre-fills a new row from the server's own defaults, in one
 * transaction. All this file names is which two kinds.
 *
 * State contract: no editable form state and no seeding effect — everything on
 * screen comes from the rules query alone, so a rule changed anywhere else on
 * the page shows up here on the next poll with nothing to keep in sync.
 *
 * ONE INSTANCE PER ACCOUNT, and this file guarantees it rather than asking its
 * caller to. The export below is a shell whose only job is to key the body on
 * the account id, so switching accounts unmounts the body and mounts a fresh
 * one. That is not cosmetic: the button issues two writes with an await between
 * them, and the mutation resolves its account when it is CALLED, so a surviving
 * instance would send the second write to whichever account the operator moved
 * to — enabling an engine, and its OnlyFans writes, on an account nobody asked
 * about. Remounting makes the whole class unreachable, and takes any failure
 * message with it. A caller that forgot the key would silently reopen that, so
 * the key is not theirs to forget.
 */

import { useState } from "react";

import { Button } from "@/components/ui/primitives";
import { useAutomationRules, useSwitchKind } from "@/hooks/useAutomations";

/** The two kinds the button owns, and how the failure line names each half.
 *
 *  Nothing else about them lives here — not the cadence, not the rule name, not
 *  what a new row's payload should be. Name and cadence come from the kind's
 *  catalog entry, and the create payload from `_CREATE_PAYLOAD` beside it (which
 *  is deliberately NOT what the rules editor's form produces). The switch route
 *  reads both on create. */
const KINDS: { kind: string; label: string }[] = [
  { kind: "gen_info", label: "profile building" },
  { kind: "apply_profiles", label: "applying" },
];

export default function FanProfilesSection({ accountId }: { accountId: string | null }) {
  // The whole of the shell: see the "ONE INSTANCE PER ACCOUNT" note above.
  return <FanProfilesBody key={accountId ?? "none"} accountId={accountId} />;
}

function FanProfilesBody({ accountId }: { accountId: string | null }) {
  // Same query key as the panel's own rules list, so this subscribes to the
  // cached result rather than issuing a second request.
  const rulesQ = useAutomationRules(accountId);
  const switchKind = useSwitchKind(accountId);
  // Only failures are held. Success has a voice already — the derived line
  // below, which follows the rules — and a stored "Fan profiles are on." would
  // sit there unchanged after the operator parks one of the rules in the list
  // on this same page, contradicting the line that had just gone away.
  const [err, setErr] = useState<string | null>(null);

  // "On" means each kind has a RUNNING row — the same row the server wakes, so
  // the label cannot disagree with what the button would do. Before any read
  // lands there is no data, so this is false: a pending read claims nothing.
  const isOn =
    KINDS.every((k) => !!rulesQ.data?.some((r) => r.kind === k.kind && r.is_enabled));
  // Nothing may be written against rules we have not read — and "have not read"
  // means no rows, not an unhappy query. A background fetch failure sets
  // `status: "error"` while KEEPING the rows (query-core sets it unconditionally
  // and exposes `isRefetchError = isError && hasData` for exactly this state), so
  // keying this on `isSuccess` parked the button for 30s every time one poll
  // blipped, against rows that were still on screen and still true.
  //
  // `useRuleView.settling` one directory over is keyed on data presence for the
  // same reason. It also carries an `isPlaceholderData` term, which this does
  // not need: that card lives through an account switch, while this component is
  // replaced by one (see the shell above), so it never renders another account's
  // rows in the first place.
  //
  // There is no stale-account case to defend here: the account cannot change
  // under this component, only replace it (see the shell above).
  const ready = rulesQ.data !== undefined;

  async function enableAll() {
    // `ready` is re-checked here and not only in the `disabled` prop: the write
    // is what must not happen against rules we have not read, and a guard that
    // lives only in a prop is one refactor away from being bypassed.
    if (!accountId || !ready) return;
    setErr(null);
    const failures: string[] = [];
    // One at a time, and every kind is attempted even after one fails: the two
    // are independent rows, so stopping at the first would make one bad request
    // cost two. Sequential rather than parallel because these are writes to one
    // SQLite file — the order itself carries no meaning.
    for (const k of KINDS) {
      try {
        await switchKind.mutateAsync({ kind: k.kind, enable: true });
      } catch (e) {
        failures.push(`${k.label} failed: ${(e as Error)?.message || "unknown"}`);
      }
    }
    if (failures.length) {
      setErr(`${failures.join(" · ")} — press the button to retry.`);
    }
  }

  return (
    <div className="space-y-2 border-t border-border pt-3">
      <div className="flex items-center justify-between gap-2">
        <span className="text-[11px] uppercase tracking-wide text-fg-dim">Fan profiles</span>
        <Button
          size="sm"
          variant="primary"
          onClick={enableAll}
          disabled={switchKind.isPending || !accountId || !ready || isOn}
        >
          {switchKind.isPending
            ? "Turning on…"
            : isOn
            ? "Fan profiles are on"
            : "Turn on fan profiles"}
        </Button>
      </div>
      <p className="text-[11px] text-fg-dim">
        Reads each fan&apos;s chat and builds a profile — age, job, location, what
        they&apos;re into — then writes a nickname and a short fact-sheet note onto
        the fan. The fan drawer and every AI prompt read them. On a new setup they
        also go onto OnlyFans as the creator-side nickname and note (fans never see
        those; they replace anything set there by hand). Schedules, sweep sizes and
        the OnlyFans push are tunable in the Automation rules list on this page.
      </p>
      {isOn && (
        <p className="text-[11px] text-fg-dim">On — profiles are being built and applied.</p>
      )}
      {/* `isLoadingError` is "errored with no data" — a first read that never
          landed. Plain `isError` would also cover a failed background poll, which
          keeps its rows, and would print this directly beneath "On — profiles are
          being built and applied." */}
      {rulesQ.isLoadingError && (
        <div className="text-xs text-err">
          Couldn&apos;t read this account&apos;s automations, so this can&apos;t say
          whether profiles are on.
        </div>
      )}
      {err && <div className="text-xs text-err">{err}</div>}
    </div>
  );
}
