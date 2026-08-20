"use client";

/**
 * ManagedAgencyKeysCard — Setup → set OTHER agencies' AI keys. Founder only.
 *
 * The managed flow this deployment runs: an agency links its model and the
 * founder does everything else, keys included. That was already possible at
 * /admin/manage → AI keys, but nothing in the app links to that page — you had
 * to know the URL — so in practice it did not exist. This puts the same form
 * where someone looking for it actually looks: next to their own keys.
 *
 * Two things make the list usable rather than a wall of names:
 *
 *   • It is ordered and filtered by whether an agency has a model that can
 *     TALK. A dead session sends nothing, so a key pasted under its owner buys
 *     nothing until the session is re-captured — and most agencies here are in
 *     that state. Default ON, because the default question is "who is worth a
 *     key right now", not "who exists".
 *   • `needs a key` marks the row that is actually costing something: a live
 *     model with no credential for it to run on.
 *
 * One agency is expanded at a time. Each carries its own admin-password field
 * because the write is founder-gated per request — there is no session-wide
 * "unlocked" state to get wrong, and a password typed for one agency cannot
 * leak into a save for another.
 */

import { useCallback, useEffect, useState } from "react";

import { relay } from "@/lib/relay";
import { Badge, Card } from "@/components/ui/primitives";
import { AgencyKeysForm } from "@/components/setup/AgencyKeysForm";

interface AgencyKeyStatus {
  user_id: string;
  username: string;
  is_admin: boolean;
  accounts: number;
  live_accounts: number;
  providers_set: string[];
}

export default function ManagedAgencyKeysCard() {
  const [agencies, setAgencies] = useState<AgencyKeyStatus[] | null>(null);
  const [liveOnly, setLiveOnly] = useState(true);
  const [openId, setOpenId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const data = await relay.get<{ agencies: AgencyKeyStatus[] }>(
        "/admin/users/llm-key-status",
      );
      setAgencies(data.agencies);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  // A 403 here is the normal case for everyone who is not the founder, so the
  // card renders nothing at all rather than an error someone can do nothing
  // about. The relay stays the authority; this only decides what to draw.
  if (error) return null;
  if (agencies === null) {
    return (
      <Card className="space-y-2">
        <h2 className="text-lg font-semibold">Other agencies&apos; AI keys</h2>
        <div className="text-sm text-fg-dim">Loading…</div>
      </Card>
    );
  }

  const shown = liveOnly ? agencies.filter((a) => a.live_accounts > 0) : agencies;
  const needing = agencies.filter(
    (a) => a.live_accounts > 0 && a.providers_set.length === 0,
  ).length;

  return (
    <Card className="space-y-4">
      <div>
        <div className="flex items-center gap-2">
          <h2 className="text-lg font-semibold">Other agencies&apos; AI keys</h2>
          <Badge color="muted">founder only</Badge>
        </div>
        <p className="text-sm text-fg-dim">
          Set the key an agency&apos;s models bill, on their behalf. Their models
          stay silent until one is stored — the relay refuses rather than spend
          anyone else&apos;s credential.
        </p>
        {needing > 0 && (
          <p className="text-[11px] text-warn mt-1">
            {needing} {needing === 1 ? "agency has" : "agencies have"} a model
            that can talk and no key for it.
          </p>
        )}
      </div>

      <label className="flex items-center gap-2 text-xs text-fg-dim">
        <input
          type="checkbox"
          checked={liveOnly}
          onChange={(e) => setLiveOnly(e.target.checked)}
        />
        Only agencies with a live model session
        <span className="text-fg-dim/70">
          ({shown.length} of {agencies.length})
        </span>
      </label>

      {shown.length === 0 ? (
        <div className="text-sm text-fg-dim">
          No agency has a live model session right now.
        </div>
      ) : (
        <ul className="divide-y divide-fg/10 border border-fg/10 rounded-lg">
          {shown.map((a) => {
            const open = openId === a.user_id;
            const needsKey = a.live_accounts > 0 && a.providers_set.length === 0;
            return (
              <li key={a.user_id} className="p-3 space-y-2">
                <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
                  <span className="font-medium text-sm">@{a.username}</span>
                  {a.is_admin && <Badge color="muted">you</Badge>}
                  <span className="text-xs">
                    {a.live_accounts > 0 ? (
                      <span className="text-ok">{a.live_accounts} live</span>
                    ) : (
                      <span className="text-fg-dim/70">no live model</span>
                    )}
                  </span>
                  <span className="text-xs text-fg-dim">
                    of {a.accounts} model{a.accounts === 1 ? "" : "s"}
                  </span>
                  <span className="text-xs">
                    {a.providers_set.length > 0 ? (
                      <span className="text-fg-dim">
                        key: {a.providers_set.join(", ")}
                      </span>
                    ) : needsKey ? (
                      <span className="text-warn">needs a key</span>
                    ) : (
                      <span className="text-fg-dim/70">no key</span>
                    )}
                  </span>
                  <button
                    type="button"
                    className="ml-auto text-xs text-fg-dim hover:text-fg"
                    onClick={() => setOpenId(open ? null : a.user_id)}
                  >
                    {open ? "Cancel" : "Set keys"}
                  </button>
                </div>
                {open && (
                  <AgencyKeysForm
                    user={{ id: a.user_id, username: a.username }}
                    onSaved={() => void load()}
                  />
                )}
              </li>
            );
          })}
        </ul>
      )}
    </Card>
  );
}
