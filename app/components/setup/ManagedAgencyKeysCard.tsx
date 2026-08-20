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

import { relay, RelayError } from "@/lib/relay";
import { Badge, Card } from "@/components/ui/primitives";
import { AgencyKeyBadges, needsKey } from "@/components/setup/AgencyKeyBadges";
import { useUser } from "@/contexts/UserContext";
import { AgencyKeysForm } from "@/components/setup/AgencyKeysForm";

interface AgencyKeyStatus {
  user_id: string;
  username: string;
  is_admin: boolean;
  accounts: number;
  live_accounts: number;
  blocked_accounts: number;
  providers_set: string[];
  missing_providers: string[];
}

export default function ManagedAgencyKeysCard() {
  const { user: me } = useUser();
  const [agencies, setAgencies] = useState<AgencyKeyStatus[] | null>(null);
  const [liveOnly, setLiveOnly] = useState(true);
  const [openId, setOpenId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [forbidden, setForbidden] = useState(false);

  const load = useCallback(async () => {
    setError(null);
    try {
      const data = await relay.get<{ agencies: AgencyKeyStatus[] }>(
        "/admin/users/llm-key-status",
      );
      setAgencies(data.agencies);
    } catch (err) {
      // 401/403 is the ordinary non-founder answer and means "draw nothing".
      // ANYTHING else is a real failure and must stay on screen: this same
      // `load` runs after a successful save, and folding a 500 into the
      // not-a-founder branch would delete the card — and the form inside it —
      // moments after the write landed, leaving no note, no row, and no way
      // back short of a page reload.
      // 401 as well as 403: require_master refuses an absent principal and a
      // non-master on different codes, and both mean "not for you".
      if (err instanceof RelayError && (err.status === 401 || err.status === 403)) {
        setForbidden(true);
        return;
      }
      setError(err instanceof Error ? err.message : String(err));
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  // Not a founder: draw nothing at all rather than an error nobody can act on.
  if (forbidden) return null;
  if (agencies === null) {
    return (
      <Card className="space-y-2">
        <h2 className="text-lg font-semibold">Other agencies&apos; AI keys</h2>
        {error ? (
          <div className="text-sm text-err" role="alert">
            Couldn&apos;t load the agency list — {error}{" "}
            <button type="button" className="underline" onClick={() => void load()}>
              Retry
            </button>
          </div>
        ) : (
          <div className="text-sm text-fg-dim">Loading…</div>
        )}
      </Card>
    );
  }

  // OTHER agencies — the card says so, and "Your AI keys" one card up already
  // owns this founder's own row. Listing it twice on one page would give the
  // same stored key two write paths whose caches never hear about each other,
  // so whichever card you did not save through keeps showing the old hint.
  const others = agencies.filter((a) => a.user_id !== me?.user_id);
  const shown = liveOnly ? others.filter((a) => a.live_accounts > 0) : others;
  const needing = others.filter(needsKey).length;

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

      {error && (
        <div className="text-xs text-err" role="alert">
          Couldn&apos;t refresh the list — {error}{" "}
          <button type="button" className="underline" onClick={() => void load()}>
            Retry
          </button>
        </div>
      )}

      <label className="flex items-center gap-2 text-xs text-fg-dim">
        <input
          type="checkbox"
          checked={liveOnly}
          onChange={(e) => setLiveOnly(e.target.checked)}
        />
        Only agencies with a live model session
        <span className="text-fg-dim/70">
          ({shown.length} of {others.length})
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
            return (
              <li key={a.user_id} className="p-3 space-y-2">
                <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
                  <span className="font-medium text-sm">@{a.username}</span>
                  <AgencyKeyBadges agency={a} />
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
