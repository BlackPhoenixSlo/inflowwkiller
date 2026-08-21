"use client";

/**
 * TrackingLinksTab — Growth → Tracking Links.
 *
 * Two kinds, OnlyFans-native first (the default the operator asked for):
 *
 *  1. OnlyFans tracking links (/campaigns) — OF mints a short profile link and
 *     reports real CLICKS and, crucially, how many of those clicks SUBSCRIBED.
 *     True subscriber attribution. The share URL (onlyfans.com/<user>/c<code>)
 *     is a strongly-implied format OF's API never returns — confirm it with one
 *     real click; `url_verified:false` says so in the UI until then.
 *
 *  2. Custom redirect links (internal /t/<slug>) — for a NON-profile target
 *     (a Linktree, a Twitter, a landing page). We count clicks; we can't see
 *     subscribers, because the visitor never lands on OnlyFans.
 */

import { useState } from "react";

import { Badge, Button, Card, Input } from "@/components/ui/primitives";
import { ConfirmDeleteButton, CopyButton, Field, errMsg } from "@/components/growth/_bits";
import {
  useOfTrackingLinks, useCreateOfTrackingLink, useDeleteOfTrackingLink,
  useTrackingLinks, useCreateTrackingLink, useDeleteTrackingLink, useTrackingAnalytics,
} from "@/hooks/useGrowth";

/** The internal link is same-origin (Next rewrites /t/* to the relay). */
function publicUrl(path: string): string {
  if (typeof window === "undefined") return path;
  return `${window.location.origin}${path}`;
}

export default function TrackingLinksTab({ accountId }: { accountId: string | null }) {
  return (
    <div className="space-y-8 max-w-3xl">
      <OfTracking accountId={accountId} />
      <CustomRedirects accountId={accountId} />
    </div>
  );
}

// ── OnlyFans-native (default) ─────────────────────────────────────────

function OfTracking({ accountId }: { accountId: string | null }) {
  const q = useOfTrackingLinks(accountId);
  const createM = useCreateOfTrackingLink(accountId);
  const deleteM = useDeleteOfTrackingLink(accountId);

  const [name, setName] = useState("");
  const [err, setErr] = useState<string | null>(null);

  const links = q.data?.links ?? [];
  const ofAvailable = q.data?.of_available ?? false;
  const urlVerified = q.data?.url_verified ?? false;

  async function create() {
    setErr(null);
    if (!name.trim()) { setErr("Name is required."); return; }
    try {
      await createM.mutateAsync({ name: name.trim() });
      setName("");
    } catch (e) { setErr(errMsg(e, "Create failed")); }
  }

  return (
    <section className="space-y-4">
      <header>
        <div className="flex items-center gap-2">
          <h2 className="text-lg font-semibold">Tracking Links</h2>
          <Badge color="ok">OnlyFans</Badge>
        </div>
        <p className="text-sm text-fg-dim">
          OnlyFans&rsquo; own tracking links for your profile. Name one per channel
          (TikTok, Reddit, a specific shoutout) and OnlyFans shows how many people
          clicked <b>and how many subscribed</b> — real attribution.
          {!ofAvailable && (
            <span className="text-warn"> · OnlyFans not reachable — showing nothing.</span>
          )}
        </p>
      </header>

      {!urlVerified && links.length > 0 && (
        <div className="rounded-lg border border-warn/40 bg-warn/5 p-3 text-[12px] text-fg-dim">
          <b className="text-fg">Confirm the link once.</b> OnlyFans doesn&rsquo;t expose the
          share URL through its API, so the address below is our best construction.
          Click one — if it opens your profile, the format is right for every link here.
        </div>
      )}

      <Card className="p-4 space-y-3">
        <div className="grid gap-3 sm:grid-cols-[1fr_auto] items-end">
          <Field label="Channel name">
            <Input value={name} onChange={(e) => setName(e.target.value)}
              placeholder="e.g. TikTok bio" />
          </Field>
          <Button size="sm" onClick={create} disabled={createM.isPending || !accountId}>
            {createM.isPending ? "Creating…" : "Create link"}
          </Button>
        </div>
        {err && <div className="text-err text-xs">{err}</div>}
      </Card>

      {q.isLoading && <div className="text-sm text-fg-dim">Loading…</div>}
      {!q.isLoading && ofAvailable && links.length === 0 && (
        <div className="text-sm text-fg-dim">No OnlyFans tracking links yet.</div>
      )}

      <ul className="divide-y divide-border">
        {links.map((l) => (
          <li key={l.id ?? l.code} className="py-3 flex flex-wrap items-center gap-3">
            <div className="min-w-0 flex-1">
              <div className="text-sm font-medium text-fg truncate">{l.name}</div>
              <div className="text-[11px] text-fg-dim truncate">
                <span className="text-accent">{l.url ?? "(url unavailable)"}</span>
              </div>
            </div>
            <div className="text-sm tabular-nums text-fg shrink-0 text-right">
              <div><b>{l.subscribers}</b> <span className="text-fg-dim text-[11px]">subs</span></div>
              <div className="text-fg-dim text-[11px]">{l.clicks} clicks</div>
            </div>
            <div className="flex items-center gap-1.5 shrink-0">
              {l.url && <CopyButton text={l.url} label="Copy link" />}
              {l.id != null && (
                <ConfirmDeleteButton
                  confirm={`Delete tracking link "${l.name}"?`}
                  onConfirm={() => deleteM.mutate(l.id!)}
                  pending={deleteM.isPending}
                />
              )}
            </div>
          </li>
        ))}
      </ul>
    </section>
  );
}

// ── Internal redirect links (secondary) ───────────────────────────────

function CustomRedirects({ accountId }: { accountId: string | null }) {
  const linksQ = useTrackingLinks(accountId);
  const createM = useCreateTrackingLink(accountId);
  const deleteM = useDeleteTrackingLink(accountId);

  const [name, setName] = useState("");
  const [target, setTarget] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [openId, setOpenId] = useState<number | null>(null);
  const [open, setOpen] = useState(false);

  const links = linksQ.data ?? [];

  async function create() {
    setErr(null);
    if (!name.trim()) { setErr("Name is required."); return; }
    if (!/^https?:\/\//.test(target.trim())) { setErr("Target must be an http(s) URL."); return; }
    try {
      await createM.mutateAsync({ name: name.trim(), target_url: target.trim() });
      setName(""); setTarget("");
    } catch (e) { setErr(errMsg(e, "Create failed")); }
  }

  return (
    <section className="space-y-4 border-t border-border pt-6">
      <header>
        <button type="button" onClick={() => setOpen((v) => !v)}
          className="flex items-center gap-2 text-left">
          <h2 className="text-lg font-semibold">Custom redirect links</h2>
          <Badge color="muted">/t/&lt;slug&gt;</Badge>
          <span className="text-fg-dim text-xs">{open ? "▲" : "▼"}</span>
          {links.length > 0 && <span className="text-fg-dim text-xs">· {links.length}</span>}
        </button>
        <p className="text-sm text-fg-dim">
          For a target that <i>isn&rsquo;t</i> your OnlyFans profile — a Linktree, a
          landing page, a link you want to swap later. We count clicks; subscriber
          attribution isn&rsquo;t possible here, since the visitor never lands on OnlyFans.
        </p>
      </header>

      {open && (
        <>
          <Card className="p-4 space-y-3">
            <div className="grid gap-3 sm:grid-cols-2">
              <Field label="Name">
                <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. Linktree" />
              </Field>
              <Field label="Target URL">
                <Input value={target} onChange={(e) => setTarget(e.target.value)}
                  placeholder="https://linktr.ee/creator" />
              </Field>
            </div>
            <div className="flex items-center gap-2">
              {err && <span className="text-err text-xs mr-auto">{err}</span>}
              <Button size="sm" onClick={create} disabled={createM.isPending || !accountId}
                className={err ? "" : "ml-auto"}>
                {createM.isPending ? "Creating…" : "Create link"}
              </Button>
            </div>
          </Card>

          {!linksQ.isLoading && links.length === 0 && (
            <div className="text-sm text-fg-dim">No custom redirect links yet.</div>
          )}

          <ul className="divide-y divide-border">
            {links.map((l) => (
              <li key={l.id} className="py-3 space-y-2">
                <div className="flex flex-wrap items-center gap-3">
                  <div className="min-w-0 flex-1">
                    <div className="text-sm font-medium text-fg truncate">{l.name}</div>
                    <div className="text-[11px] text-fg-dim truncate">
                      <span className="text-accent">{l.path}</span> → {l.target_url}
                    </div>
                  </div>
                  <div className="text-sm tabular-nums text-fg shrink-0">
                    {l.click_count} <span className="text-fg-dim text-[11px]">clicks</span>
                  </div>
                  <div className="flex items-center gap-1.5 shrink-0">
                    <CopyButton text={publicUrl(l.path)} label="Copy link" />
                    <Button size="sm" variant="ghost" onClick={() => setOpenId(openId === l.id ? null : l.id)}>
                      {openId === l.id ? "Hide" : "Analytics"}
                    </Button>
                    <ConfirmDeleteButton
                      confirm={`Delete redirect link "${l.name}"?`}
                      onConfirm={() => deleteM.mutate(l.id)}
                      pending={deleteM.isPending}
                    />
                  </div>
                </div>
                {openId === l.id && accountId && <Analytics accountId={accountId} linkId={l.id} />}
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}

function Analytics({ accountId, linkId }: { accountId: string; linkId: number }) {
  const q = useTrackingAnalytics(accountId, linkId);
  if (q.isLoading) return <div className="text-[11px] text-fg-dim ml-1">Loading analytics…</div>;
  if (q.isError || !q.data) return <div className="text-[11px] text-err ml-1">Failed to load analytics.</div>;
  const a = q.data;
  const max = Math.max(1, ...a.by_day.map((d) => d.clicks));
  return (
    <div className="ml-1 rounded-lg border border-border bg-bg/40 p-3 space-y-2">
      <div className="text-sm text-fg">
        <b>{a.total_clicks}</b> total clicks
        <span className="text-fg-dim text-[11px]"> · subscribers not tracked for off-profile links</span>
      </div>
      {a.by_day.length > 0 ? (
        <div className="flex items-end gap-1 h-16">
          {a.by_day.map((d) => (
            <div key={d.day} className="flex-1 min-w-2 flex flex-col items-center justify-end" title={`${d.day}: ${d.clicks}`}>
              <div className="w-full bg-accent/70 rounded-t" style={{ height: `${(d.clicks / max) * 100}%` }} />
              <span className="text-[8px] text-fg-dim mt-0.5 truncate w-full text-center">{d.day.slice(5)}</span>
            </div>
          ))}
        </div>
      ) : (
        <div className="text-[11px] text-fg-dim">No clicks yet.</div>
      )}
    </div>
  );
}
