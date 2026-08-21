"use client";

/**
 * TrialLinksTab — Growth → Trial Links.
 *
 * Mint / label / delete OnlyFans free-trial links and read their redemptions.
 * OF is the source of truth; a local mirror keeps the operator's name so the
 * table renders even when a live OF fetch is unavailable.
 */

import { useState } from "react";

import { Badge, Button, Card, Input } from "@/components/ui/primitives";
import { ConfirmDeleteButton, CopyButton, Field, NumInput, errMsg } from "@/components/growth/_bits";
import {
  useTrialLinks, useCreateTrialLink, useDeleteTrialLink,
} from "@/hooks/useGrowth";

export default function TrialLinksTab({ accountId }: { accountId: string | null }) {
  const linksQ = useTrialLinks(accountId);
  const createM = useCreateTrialLink(accountId);
  const deleteM = useDeleteTrialLink(accountId);

  const [name, setName] = useState("");
  const [days, setDays] = useState(7);
  const [counts, setCounts] = useState(1);
  const [err, setErr] = useState<string | null>(null);

  const links = linksQ.data?.links ?? [];
  const ofAvailable = linksQ.data?.of_available ?? false;

  async function create() {
    setErr(null);
    if (!name.trim()) { setErr("Name is required."); return; }
    try {
      await createM.mutateAsync({ name: name.trim(), subscribe_days: days, subscribe_counts: counts });
      setName("");
    } catch (e) { setErr(errMsg(e, "Create failed")); }
  }

  return (
    <div className="space-y-4 max-w-3xl">
      <header>
        <h2 className="text-lg font-semibold">Free-Trial Links</h2>
        <p className="text-sm text-fg-dim">
          Mint free-trial links on OnlyFans, name them, and track redemptions.
          {!ofAvailable && (
            <span className="text-warn"> · OnlyFans not reachable — showing saved links only.</span>
          )}
        </p>
      </header>

      <Card className="p-4 space-y-3">
        <div className="grid gap-3 sm:grid-cols-4">
          <div className="sm:col-span-2">
            <Field label="Name">
              <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. Twitter 7-day" />
            </Field>
          </div>
          <Field label="Free days">
            <NumInput value={days} min={1} onChange={setDays} />
          </Field>
          {/* 0 = unlimited claims (OF accepts it). Days has no such option —
            *  OF rejects subscribeDays < 1 outright. */}
          <Field label="Max claims (0 = unlimited)">
            <NumInput value={counts} min={0} onChange={setCounts} />
          </Field>
        </div>
        <div className="flex items-center gap-2">
          {err && <span className="text-err text-xs mr-auto">{err}</span>}
          <Button size="sm" onClick={create} disabled={createM.isPending || !accountId}
            className={err ? "" : "ml-auto"}>
            {createM.isPending ? "Creating…" : "Create trial link"}
          </Button>
        </div>
      </Card>

      {linksQ.isLoading && <div className="text-sm text-fg-dim">Loading…</div>}
      {!linksQ.isLoading && links.length === 0 && (
        <div className="text-sm text-fg-dim">No trial links yet.</div>
      )}

      <ul className="divide-y divide-border">
        {links.map((l, i) => (
          <li key={l.id ?? `of-${l.of_trial_id ?? i}`} className="py-3 flex flex-wrap items-center gap-3">
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2">
                <span className="text-sm font-medium text-fg truncate">{l.name || "(unnamed)"}</span>
                {/* A spent link keeps showing in OF's list — say so, or a dead
                 *  link reads exactly like a live one. */}
                {l.status === "finished" && <Badge color="muted">spent</Badge>}
                {l.status === "active" && <Badge color="ok">live</Badge>}
                {l.source === "onlyfans" && <Badge color="muted">OnlyFans</Badge>}
              </div>
              <div className="text-[11px] text-fg-dim truncate">
                {l.url || "no url"} · {l.subscribe_days}-day ·{" "}
                {l.subscribe_counts
                  ? `${l.claim_counts}/${l.subscribe_counts} claimed`
                  : `${l.claim_counts} claimed (unlimited)`}
                {l.clicks_count != null && ` · ${l.clicks_count} click${l.clicks_count === 1 ? "" : "s"}`}
                {l.expired_at && ` · expired ${new Date(l.expired_at).toLocaleDateString()}`}
              </div>
            </div>
            <div className="flex items-center gap-1.5 shrink-0">
              {l.url && <CopyButton text={l.url} label="Copy link" />}
              {l.id != null && (
                <ConfirmDeleteButton
                  confirm={`Delete trial link "${l.name}"?`}
                  onConfirm={() => deleteM.mutate(l.id!)}
                  pending={deleteM.isPending}
                />
              )}
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}
