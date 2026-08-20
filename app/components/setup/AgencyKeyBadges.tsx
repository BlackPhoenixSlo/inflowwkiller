"use client";

/**
 * AgencyKeyBadges — the three facts that decide whether an agency needs a key,
 * rendered identically wherever agencies are listed (Setup and Admin → Manage).
 *
 * It existed twice, in two colour systems (emerald/amber literals on one screen,
 * the ok/warn tokens on the other), which is how "needs a key" ends up meaning
 * two different things on two pages. One definition, one palette.
 *
 * The counts are BILLED, not linked — `tenant_keys.key_overview` resolves
 * ownership the way the money path does, so `live` here is "models that will
 * spend THIS agency's key", never "models it is merely linked to".
 */

import { Badge } from "@/components/ui/primitives";

export interface AgencyKeyFacts {
  accounts: number;
  live_accounts: number;
  blocked_accounts: number;
  providers_set: string[];
}

/** True when this agency is costing something right now: a model that can talk
 *  and no credential for it to talk on. */
export function needsKey(a: AgencyKeyFacts): boolean {
  return a.live_accounts > 0 && a.providers_set.length === 0;
}

export function AgencyKeyBadges({ agency }: { agency: AgencyKeyFacts }) {
  return (
    <>
      <span className="text-xs">
        {agency.live_accounts > 0 ? (
          <span className="text-ok">{agency.live_accounts} live</span>
        ) : (
          <span className="text-fg-dim/70">no live model</span>
        )}
      </span>
      <span className="text-xs text-fg-dim">
        of {agency.accounts} model{agency.accounts === 1 ? "" : "s"}
      </span>
      <span className="text-xs">
        {agency.providers_set.length > 0 ? (
          <span className="text-fg-dim">key: {agency.providers_set.join(", ")}</span>
        ) : needsKey(agency) ? (
          <span className="text-warn">needs a key</span>
        ) : (
          <span className="text-fg-dim/70">no key</span>
        )}
      </span>
      {agency.blocked_accounts > 0 && (
        // No key fixes these — two agencies claim the model, so the relay
        // refuses it outright. Said here because a keys screen is exactly where
        // someone would otherwise paste a key and wonder why nothing changed.
        <Badge color="err">
          {agency.blocked_accounts} blocked — two owners
        </Badge>
      )}
    </>
  );
}
