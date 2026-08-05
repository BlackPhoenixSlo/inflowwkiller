"use client";

/**
 * ReengageBuyersTab — Automations → "💌 Re-engage buyers".
 *
 * Win back recent buyers who went cold: fans who bought in the last N days but got no
 * follow-up. She sends ONE warm, PERSONAL opener built from their own stored gen_info
 * lines (teases/questions + name), then the normal AI Chatter / Upseller takes over.
 *
 * Manual by design: Preview (dry-run) shows the exact openers, Send now fires them.
 * Preview → POST /admin/reengage-preview (sync). Send → /admin/automation/enqueue.
 */

import { useState } from "react";

import { Button, Card, Input } from "@/components/ui/primitives";
import { relay } from "@/lib/relay";
import { useEmployee } from "@/contexts/EmployeeContext";

interface PreviewRow { fan_id: number; name: string; opener: string }
interface PreviewResp {
  dry_run: boolean; candidates: number; would_send: number; tone: string;
  preview: PreviewRow[]; skipped?: string;
}

const INPUT = "bg-bg border border-border rounded-lg px-2 py-1.5 text-base md:text-sm w-full focus:outline-none focus:border-accent";

function NumField({ label, hint, value, onChange }: {
  label: string; hint?: string; value: number; onChange: (n: number) => void;
}) {
  return (
    <label className="flex flex-col gap-1 text-[11px] text-fg-dim">
      <span className="leading-tight">{label} {hint && <span className="opacity-60">({hint})</span>}</span>
      <Input type="number" min={0} value={value}
        onChange={(e) => onChange(Math.max(0, parseInt(e.target.value || "0", 10) || 0))} />
    </label>
  );
}

export default function ReengageBuyersTab({ accountId }: { accountId: string | null }) {
  const { current: employee } = useEmployee();
  const [lookbackDays, setLookbackDays] = useState(3);
  const [coldHours, setColdHours] = useState(24);
  const [maxPerRun, setMaxPerRun] = useState(25);
  const [guardHours, setGuardHours] = useState(12);
  const [tone, setTone] = useState<"soft" | "flirty">("soft");

  const [preview, setPreview] = useState<PreviewResp | null>(null);
  const [busy, setBusy] = useState<"" | "preview" | "send">("");
  const [error, setError] = useState<string | null>(null);
  const [okMsg, setOkMsg] = useState<string | null>(null);

  const settings = { lookback_days: lookbackDays, cold_hours: coldHours,
    max_per_run: maxPerRun, guard_hours: guardHours, tone };

  async function runPreview() {
    if (!accountId) { setError("Pick an account first."); return; }
    setError(null); setOkMsg(null); setBusy("preview");
    try {
      const resp = await relay.post<PreviewResp>(
        "/admin/reengage-preview", { account_id: accountId, ...settings },
        { accountId, employeeId: employee?.id });
      setPreview(resp);
    } catch (e) {
      setError((e as Error)?.message || "Preview failed");
    } finally { setBusy(""); }
  }

  async function sendNow() {
    if (!accountId) { setError("Pick an account first."); return; }
    setError(null); setOkMsg(null);
    // Always know the REAL count before confirming — run a fresh dry-run if the
    // operator didn't Preview first, so the confirm never shows a misleading cap.
    setBusy("send");
    let n: number;
    try {
      const pv = await relay.post<PreviewResp>(
        "/admin/reengage-preview", { account_id: accountId, ...settings },
        { accountId, employeeId: employee?.id });
      setPreview(pv);
      n = pv.would_send ?? 0;
    } catch (e) {
      setBusy(""); setError((e as Error)?.message || "Couldn't check who's cold"); return;
    }
    if (n === 0) {
      setBusy(""); setOkMsg("No cold buyers on this account right now — nothing sent.");
      return;
    }
    if (!window.confirm(
      `Send a personal re-engage message to ${n} cold buyer(s) on this account now?` +
      `\n\nThis sends real DMs (${tone} tone). Cancel to review the preview first.`)) {
      setBusy(""); return;
    }
    try {
      const resp = await relay.post<{ enqueued_job_id: number }>(
        "/admin/automation/enqueue",
        { account_id: accountId, kind: "reengage_buyers", payload: { ...settings } },
        { accountId, employeeId: employee?.id });
      setOkMsg(`Queued (job #${resp.enqueued_job_id}) for ${n} fan(s) — sends within ~30s. Refresh the inbox to watch.`);
    } catch (e) {
      setError((e as Error)?.message || "Send failed");
    } finally { setBusy(""); }
  }

  if (!accountId) return <div className="text-sm text-fg-dim">Pick an account above.</div>;

  return (
    <div className="space-y-4">
      <div>
        <h3 className="text-sm font-medium text-fg">Re-engage buyers who went cold</h3>
        <p className="text-xs text-fg-dim leading-relaxed mt-1">
          Finds fans who <b>bought recently</b> but got <b>no follow-up</b>, and sends
          each ONE warm, personal opener made from his own stored lines (his teases /
          questions + name) — then the AI Chatter takes over his reply. It skips anyone
          the account messaged recently, is mid-sale with, or is on a stop list. <b>Preview
          first</b> to see the exact openers.
        </p>
      </div>

      <Card className="p-4 space-y-3">
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <NumField label="Bought within" hint="days" value={lookbackDays} onChange={setLookbackDays} />
          <NumField label="Cold after" hint="hrs no reply" value={coldHours} onChange={setColdHours} />
          <NumField label="Max this run" hint="cap" value={maxPerRun} onChange={setMaxPerRun} />
          <NumField label="Skip if messaged within" hint="hrs" value={guardHours} onChange={setGuardHours} />
        </div>
        <div className="flex items-center gap-4 text-sm">
          <span className="text-xs text-fg-dim">Tone:</span>
          <label className="flex items-center gap-1.5">
            <input type="radio" name="tone" checked={tone === "soft"} onChange={() => setTone("soft")} />
            Soft &amp; sweet
          </label>
          <label className="flex items-center gap-1.5">
            <input type="radio" name="tone" checked={tone === "flirty"} onChange={() => setTone("flirty")} />
            Flirty &amp; straight back in 🥵
          </label>
        </div>

        <div className="flex items-center gap-2 border-t border-border pt-3 flex-wrap md:flex-nowrap">
          <Button className="shrink-0 md:shrink" size="sm" variant="secondary" disabled={busy !== ""} onClick={runPreview}>
            {busy === "preview" ? "Checking…" : "Preview (dry run)"}
          </Button>
          <Button className="shrink-0 md:shrink" size="sm" variant="primary" disabled={busy !== ""} onClick={sendNow}>
            {busy === "send" ? "Queuing…" : "Send now"}
          </Button>
          {error && <span className="text-err text-[11px] basis-full md:basis-auto">{error}</span>}
          {okMsg && <span className="text-ok text-[11px] basis-full md:basis-auto">{okMsg}</span>}
        </div>
      </Card>

      {preview && (
        <Card className="p-4 space-y-2">
          <div className="text-sm">
            {preview.candidates === 0 ? (
              <span className="text-fg-dim">No cold buyers right now — nobody bought in the
                last {lookbackDays}d and went quiet.</span>
            ) : (
              <span className="text-fg">
                <b>{preview.would_send}</b> cold buyer(s) would get a message
                {preview.candidates > preview.would_send &&
                  <span className="text-fg-dim"> ({preview.candidates} found, capped/excluded to {preview.would_send})</span>}
                . Sample openers:
              </span>
            )}
          </div>
          {(preview.preview ?? []).length > 0 && (
            <ul className="space-y-1.5">
              {preview.preview.map((r) => (
                <li key={r.fan_id} className="text-sm">
                  <span className="text-fg-dim text-xs">{r.name || r.fan_id}: </span>
                  <span className="bg-accent/10 border border-accent/30 rounded-2xl px-3 py-1 inline-block whitespace-pre-line align-top">
                    {r.opener}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </Card>
      )}
    </div>
  );
}
