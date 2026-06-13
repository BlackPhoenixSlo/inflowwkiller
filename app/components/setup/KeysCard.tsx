"use client";

/**
 * KeysCard — Setup → Keys. Paste API keys and tokens here instead of editing
 * the server's .env. Saved values live on the server only (service/secrets.json)
 * and take effect immediately — no restart.
 *
 * Each field shows whether the key is currently set and where the live value
 * comes from (this UI vs the server env). The raw value is never sent back to
 * the browser; an empty field means "leave unchanged".
 */

import { useState } from "react";

import { useSecrets, useSaveSecrets, type SecretKeyStatus } from "@/hooks/useSecrets";
import { Badge, Button, Card, Input, Textarea } from "@/components/ui/primitives";

export default function KeysCard() {
  const { data, isLoading, error } = useSecrets();
  const save = useSaveSecrets();
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [note, setNote] = useState<string | null>(null);

  const keys = data?.keys ?? {};
  // Preserve backend (registry) order, bucket by group.
  const groups: { name: string; items: [string, SecretKeyStatus][] }[] = [];
  for (const entry of Object.entries(keys)) {
    const g = entry[1].group;
    let bucket = groups.find((b) => b.name === g);
    if (!bucket) {
      bucket = { name: g, items: [] };
      groups.push(bucket);
    }
    bucket.items.push(entry);
  }

  const dirty = Object.keys(draft).length > 0;

  function setField(name: string, val: string) {
    setDraft((d) => ({ ...d, [name]: val }));
  }

  async function onSave() {
    setNote(null);
    try {
      await save.mutateAsync(draft);
      setDraft({});
      setNote("Saved — takes effect on the next run.");
      setTimeout(() => setNote(null), 3000);
    } catch (e) {
      setNote(e instanceof Error ? e.message : "Save failed");
    }
  }

  async function clearKey(name: string) {
    setNote(null);
    try {
      await save.mutateAsync({ [name]: "" });
      setDraft((d) => {
        const { [name]: _drop, ...rest } = d;
        return rest;
      });
    } catch (e) {
      setNote(e instanceof Error ? e.message : "Clear failed");
    }
  }

  return (
    <Card className="space-y-4">
      <div>
        <h2 className="text-lg font-semibold">Keys</h2>
        <p className="text-sm text-fg-dim">
          API keys and tokens for this server. Paste them here instead of editing
          the VPS <code>.env</code> — they&apos;re stored on the server only and
          take effect immediately. Leave a field blank to keep its current value.
        </p>
      </div>

      {isLoading && <div className="text-sm text-fg-dim">Loading…</div>}
      {error && (
        <div className="text-sm text-err">Couldn&apos;t load key status.</div>
      )}

      {groups.map((g) => (
        <section key={g.name} className="space-y-3">
          <h3 className="text-xs font-semibold uppercase tracking-wide text-fg-dim">
            {g.name}
          </h3>
          {g.items.map(([name, k]) => {
            const val = draft[name] ?? "";
            const placeholder = k.set
              ? k.secret
                ? `${k.hint} — leave blank to keep`
                : k.hint
              : "not set";
            return (
              <div key={name} className="space-y-1">
                <div className="flex items-center gap-2">
                  <label className="text-sm font-medium">{k.label}</label>
                  {k.set ? (
                    <Badge color="ok">
                      set{k.source === "env" ? " · via env" : " · via UI"}
                    </Badge>
                  ) : (
                    <Badge color="muted">not set</Badge>
                  )}
                  {k.set && k.source === "store" && (
                    <button
                      type="button"
                      onClick={() => clearKey(name)}
                      className="text-[11px] text-err hover:underline"
                    >
                      clear
                    </button>
                  )}
                </div>
                {k.multiline ? (
                  <Textarea
                    rows={3}
                    value={val}
                    placeholder={placeholder}
                    onChange={(e) => setField(name, e.target.value)}
                    className="font-mono text-xs"
                  />
                ) : (
                  <Input
                    type={k.secret ? "password" : "text"}
                    value={val}
                    placeholder={placeholder}
                    autoComplete="off"
                    onChange={(e) => setField(name, e.target.value)}
                  />
                )}
                {k.help && <p className="text-[11px] text-fg-dim">{k.help}</p>}
              </div>
            );
          })}
        </section>
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
