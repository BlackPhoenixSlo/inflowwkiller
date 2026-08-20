"use client";

import { useCallback, useEffect, useState } from "react";

import { relay } from "@/lib/relay";
import { useKeyDraft } from "@/hooks/useKeyDraft";
import { LABELS, HELP, wrongFieldWarning } from "@/components/setup/providerFields";

/**
 * AgencyKeysForm — set an agency's LLM keys FOR them.
 *
 * The managed flow: they connect the model, the founder does the rest. The
 * self-serve card at Setup → Your AI keys only ever writes the SIGNED-IN
 * owner's row, and impersonation is read-only, so this is the only way to fix
 * an agency that never pasted one — and the way to do the whole post-upgrade
 * pass from a single screen instead of logging in as each owner.
 *
 * The edit rules are NOT restated here. A secret field is safe to edit exactly
 * one way — placeholder never holds the value, blank means unchanged even after
 * a typed-then-backspaced field, only the clear link sends "" — and useKeyDraft
 * is where that lives, shared with both Setup cards. A third hand-rolled copy
 * would be a third place for the next fix to miss.
 *
 * The founder password is the one thing that is local: it gates the write and
 * is not a key, so it never enters the draft.
 */
export function AgencyKeysForm(
  { user, onSaved }: { user: AgencyRef; onSaved?: () => void },
) {
  const [providers, setProviders] = useState<Record<string, KeyStatus> | null>(null);
  const [adminPassword, setAdminPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoadError(null);
    try {
      const data = await relay.get<{ providers: Record<string, KeyStatus> }>(
        `/admin/users/${encodeURIComponent(user.id)}/llm-keys`,
      );
      setProviders(data.providers);
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : String(err));
    }
  }, [user.id]);

  useEffect(() => { void load(); }, [load]);

  // Thrown, not swallowed: useKeyDraft surfaces a rejected submit as a sticky
  // note and keeps the draft, which is what a missing password should do.
  async function submit(values: Record<string, string>) {
    if (!adminPassword) throw new Error("Admin password required.");
    setBusy(true);
    try {
      await relay.put(`/admin/users/${encodeURIComponent(user.id)}/llm-keys`, {
        admin_password: adminPassword,
        providers: values,
      });
      await load();
      // The caller may be showing derived state about this agency (a "needs a
      // key" badge), and it is now stale.
      onSaved?.();
    } finally {
      setBusy(false);
    }
  }

  const { draft, dirty, setField, save, clear, note } = useKeyDraft(submit, "Saved.");

  return (
    <div className="rounded-lg border border-fg/10 p-3 space-y-3">
      <p className="text-xs text-fg-dim">
        Keys @{user.username}&apos;s models bill. Leave a field blank to keep the
        stored one.
      </p>
      {loadError && <div className="text-xs text-red-400" role="alert">{loadError}</div>}
      {providers === null ? (
        <div className="text-xs text-fg-dim">Loading…</div>
      ) : (
        Object.entries(providers).map(([name, k]) => (
          <div key={name} className="space-y-1">
          <div className="flex items-center gap-2">
            <label className="w-40 text-xs">{LABELS[name] ?? name}</label>
            <input
              type="password"
              autoComplete="off"
              value={draft[name] ?? ""}
              placeholder={k.set ? `${k.hint} — blank keeps it` : "not set"}
              onChange={(e) => setField(name, e.target.value)}
              className="flex-1 rounded border border-fg/20 bg-transparent px-2 py-1 text-xs"
            />
            {k.set && (
              <button
                type="button"
                disabled={busy}
                className="text-[11px] text-red-400 hover:text-red-300 disabled:opacity-40"
                onClick={() => {
                  // Someone ELSE's credential: every automation on every account
                  // they own fails closed the moment this lands, and they get no
                  // notification. Same beat the owner's own card takes.
                  if (window.confirm(`Remove @${user.username}'s ${name} key? Their models stop replying until one is added.`)) {
                    void clear(name);
                  }
                }}
              >
                clear
              </button>
            )}
          </div>
          {wrongFieldWarning(name, draft[name] ?? "") ? (
            <p className="text-[11px] text-warn" role="alert">
              {wrongFieldWarning(name, draft[name] ?? "")}
            </p>
          ) : (
            HELP[name] && <p className="text-[11px] text-fg-dim">{HELP[name]}</p>
          )}
          </div>
        ))
      )}
      <div className="flex items-center gap-2">
        <input
          type="password"
          autoComplete="off"
          value={adminPassword}
          placeholder="admin password"
          onChange={(e) => setAdminPassword(e.target.value)}
          className="flex-1 rounded border border-fg/20 bg-transparent px-2 py-1 text-xs"
        />
        <button
          type="button"
          disabled={busy || !dirty}
          onClick={() => void save()}
          className="rounded bg-fg/10 px-3 py-1 text-xs hover:bg-fg/20 disabled:opacity-40"
        >
          {busy ? "Saving…" : "Save keys"}
        </button>
        {note && <span className="text-xs text-fg-dim">{note}</span>}
      </div>
    </div>
  );
}


/** The two fields this form needs from whatever listed the agency. Deliberately
 *  NOT AdminUserRow: the Setup card lists agencies from the key roster while the
 *  Manage screen lists them from the user table, and neither should grow the
 *  other's shape just to reuse one form. */
export interface AgencyRef {
  id: string;
  username: string;
}

/** Masked per-provider state. The raw value never reaches the browser. */
export interface KeyStatus {
  set: boolean;
  hint: string;
}
