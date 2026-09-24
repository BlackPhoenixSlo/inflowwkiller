"use client";

/**
 * AgencyKeysCard — Setup → Keys, YOUR agency's LLM keys.
 *
 * Every OF account you own bills the key you paste here, so no other agency on
 * this server can spend on your credential and you cannot spend on theirs. That
 * is the whole reason this card is separate from KeysCard below it, which holds
 * the SERVER's own house keys.
 *
 * The edit rules that keep a stored key safe (placeholder-not-value, blank
 * means unchanged, send only what changed) live in useKeyDraft, shared with
 * that card.
 */

import { useTenantKeys, useSaveTenantKeys } from "@/hooks/useTenantKeys";
import { LABELS, HELP, wrongFieldWarning } from "@/components/setup/providerFields";
import { useKeyDraft } from "@/hooks/useKeyDraft";
import { Badge, Button, Card, Input } from "@/components/ui/primitives";

export default function AgencyKeysCard() {
  const { data, isLoading, error } = useTenantKeys();
  const save = useSaveTenantKeys();
  const { draft, dirty, setField, save: onSave, clear, note } = useKeyDraft(
    save.mutateAsync,
    "Saved — takes effect on the next message.",
  );

  const providers = data?.providers ?? {};
  const shared = data?.shared_accounts ?? [];

  return (
    <Card className="space-y-4">
      <div>
        <h2 className="text-lg font-semibold">Your AI keys</h2>
        <p className="text-sm text-fg-dim">
          The API keys your models&apos; AI runs on. They belong to your account
          only — every model you own uses them, and no one else on this server
          can. Leave a field blank to keep the key that&apos;s already stored.
        </p>
      </div>

      {shared.some((a) => a.contested.length > 0) && (
        <div className="rounded-md border border-err/40 bg-err/5 p-3 space-y-1">
          <p className="text-sm font-medium text-err">
            AI is stopped on{" "}
            {shared.filter((a) => a.contested.length > 0).length === 1
              ? "a provider of 1 model"
              : `a provider of ${shared.filter((a) => a.contested.length > 0).length} models`}{" "}
            — two owners, two keys
          </p>
          <p className="text-[11px] text-fg-dim">
            Two accounts are linked to each of these and BOTH have a key for the
            provider, so nothing says whose key pays and the relay refuses rather
            than bill the wrong one. Clear the key on the owner who should not
            pay, remove one owner in Admin → Manage (revoke), or have one of them
            transfer it to the other.
          </p>
          <ul className="text-[11px] text-fg-dim">
            {shared
              .filter((a) => a.contested.length > 0)
              .map((a) => (
                <li key={a.account_id}>
                  <span className="font-medium">{a.nickname}</span> — shared by{" "}
                  {a.owners.join(", ")}; both keyed for{" "}
                  {a.contested.map((p) => LABELS[p] ?? p).join(", ")}
                </li>
              ))}
          </ul>
        </div>
      )}

      {shared.some((a) => a.contested.length === 0) && (
        <div className="rounded-md border border-border bg-bg-elev-1 p-3 space-y-1">
          <p className="text-sm font-medium">
            {shared.filter((a) => a.contested.length === 0).length === 1
              ? "1 model has"
              : `${shared.filter((a) => a.contested.length === 0).length} models have`}{" "}
            two owners — AI keeps running
          </p>
          <p className="text-[11px] text-fg-dim">
            Each provider bills whichever owner has a key for it. A provider
            neither owner has set is silent until one of you adds it.
          </p>
          <ul className="text-[11px] text-fg-dim">
            {shared
              .filter((a) => a.contested.length === 0)
              .map((a) => (
                <li key={a.account_id}>
                  <span className="font-medium">{a.nickname}</span> — shared by{" "}
                  {a.owners.join(", ")}
                  {Object.keys(a.pays).length > 0 && (
                    <>
                      ; bills{" "}
                      {Object.entries(a.pays)
                        .map(([p, who]) => `${LABELS[p] ?? p} → ${who}`)
                        .join(", ")}
                    </>
                  )}
                </li>
              ))}
          </ul>
        </div>
      )}

      {isLoading && <div className="text-sm text-fg-dim">Loading…</div>}
      {error && (
        <div className="text-sm text-err">
          Couldn&apos;t load your key status — sign in as an account owner.
        </div>
      )}

      {Object.entries(providers).map(([name, k]) => (
        <div key={name} className="space-y-1">
          <div className="flex items-center gap-2">
            <label className="text-sm font-medium">{LABELS[name] ?? name}</label>
            {k.set ? <Badge color="ok">set</Badge> : <Badge color="muted">not set</Badge>}
            {k.set && (
              <button
                type="button"
                disabled={save.isPending}
                onClick={() => {
                  // One click here stops every automation on every account this
                  // owner runs — the calls fail closed with no key. Worth a beat.
                  const label = LABELS[name] ?? name;
                  if (window.confirm(`Remove your ${label}? Your models stop replying until you add one.`)) {
                    void clear(name);
                  }
                }}
                className="text-[11px] text-err hover:underline disabled:opacity-40"
              >
                clear
              </button>
            )}
          </div>
          <Input
            type="password"
            value={draft[name] ?? ""}
            placeholder={k.set ? `${k.hint} — leave blank to keep` : "not set"}
            autoComplete="off"
            onChange={(e) => setField(name, e.target.value)}
          />
          {wrongFieldWarning(name, draft[name] ?? "") ? (
            <p className="text-[11px] text-warn" role="alert">
              {wrongFieldWarning(name, draft[name] ?? "")}
            </p>
          ) : (
            HELP[name] && <p className="text-[11px] text-fg-dim">{HELP[name]}</p>
          )}
        </div>
      ))}

      <div className="flex items-center gap-3 pt-1">
        <Button onClick={onSave} disabled={!dirty || save.isPending}>
          {save.isPending ? "Saving…" : "Save changes"}
        </Button>
        {note && <span className="text-sm text-fg-dim">{note}</span>}
      </div>
    </Card>
  );
}
