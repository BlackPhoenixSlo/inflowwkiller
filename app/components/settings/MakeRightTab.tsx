"use client";

/**
 * MakeRightTab — Automations → "🤝 Make It Right" (the Resolution Agent).
 *
 * Detects when the bot got the content wrong — above all, a fan CHARGED TWICE for
 * the same content — and makes him whole: a warm apology + a few FREE unseen pieces
 * from the tip library, up to twice per fan, then hands him to an operator. Refunds
 * are flagged for a human, never moved automatically.
 *
 * ON by default on every model (house policy 2026-08-05) — "Enabled" + "Auto-send"
 * both default true; untick either to opt this account out. Preview (dry-run) →
 * POST /admin/make-right-preview shows the detected incidents + each proposed
 * make-right without sending; "Run now" enqueues the sweep immediately.
 * Config persists on account_ai_config.make_right_config_json, and saving also
 * creates/enables the account's 15-min `make_right` rule (the config alone is inert).
 */

import { useEffect, useState } from "react";

import { Button, Card, Input } from "@/components/ui/primitives";
import { SingleFolderRow, VaultFolderPicker } from "@/components/settings/VaultFolderPicker";
import { relay } from "@/lib/relay";
import { useEmployee } from "@/contexts/EmployeeContext";

interface Cfg {
  enabled?: boolean; auto_send?: boolean; lookback_days?: number;
  max_msgs_since_charge?: number;
  per_fan_cap?: number; gift_tier?: string;
  apology_caption?: string; flag_refund?: boolean; guard_hours?: number;
  // Gift size is ONE number: gift_pieces_per_step, below. gift_value_match /
  // gift_piece_value_cents / gift_min_count / gift_max_count are gone (2026-08-06)
  // — the API drops them, so a save from this tab also cleans them out of a config
  // that still carries them.
  // The exchange (apology -> N free TURNS on his replies -> PPV -> close). Default
  // is ONE turn: apology bubble + gift bubble, then it closes itself.
  free_steps?: number; gift_pieces_per_step?: number; open_with_gift?: boolean;
  /** Make the free apology piece CONTAIN what he asked for, rather than the next
   *  unseen thing off the tip shelf. Falls back to the shelf when nothing fits. */
  gift_on_subject?: boolean;
  ppv_folder?: string; ppv_price_cents?: number; ppv_caption?: string;
  nudge_hours?: number; close_hours?: number;
}
interface PreviewRow {
  fan_id: number; name?: string; incident_key: string; kind: string;
  wrongful_cents?: number; apology?: string; steps?: string[]; step?: number;
  gift_media_ids?: number[]; refund_flagged?: boolean; action: string; reason?: string;
}
interface PreviewResp {
  dry_run: boolean; preview_only_reason?: string | null; candidates: number;
  would_open?: number; operator_only?: number; in_progress?: number;
  excluded?: number; already_handled?: number; stale?: number; preview: PreviewRow[];
}

const INPUT = "w-24 bg-bg border border-border rounded-lg px-2 py-1.5 text-base md:text-sm focus:outline-none focus:border-accent";

function NumField({ label, hint, value, onChange, min = 0, max = 100000, step = 1, suffix }: {
  label: string; hint?: string; value: number; min?: number; max?: number;
  step?: number; suffix?: string; onChange: (n: number) => void;
}) {
  return (
    <label className="space-y-1 block">
      <div className="text-xs text-fg-dim">{label}</div>
      <div className="flex items-center gap-2">
        <input type="number" min={min} max={max} step={step} className={INPUT} value={value}
          onChange={(e) => onChange(Math.max(min, Math.min(Number(e.target.value) || 0, max)))} />
        {suffix && <span className="text-xs text-fg-dim">{suffix}</span>}
      </div>
      {hint && <div className="text-[11px] text-fg-dim/70">{hint}</div>}
    </label>
  );
}

/**
 * What the two turn/image fields add up to.
 *
 * They MULTIPLY, and the old labels hid it: "Free pieces 3" + "Pieces per step 3"
 * reads as three images and means nine, spread over four messages, three of which
 * arrive only if the fan keeps replying. That total is what an operator is really
 * choosing, and nothing on this tab used to say it.
 *
 * Arithmetic only, and deliberately no step NAMES: the preview panel below already
 * prints the real chain straight out of `_build_steps`, so a second, client-side
 * copy of that logic would be one that can disagree with the engine. These counts
 * can't disagree, because every shape `_build_steps` produces is `turns + 1`
 * messages and `turns × images` pieces — `open_with_gift` decides which message the
 * first gift rides on, never how many there are.
 */
function ExchangeShape({ form }: { form: Cfg }) {
  const turns = Math.max(0, form.free_steps ?? 1);
  const per = Math.max(1, form.gift_pieces_per_step ?? 1);
  const ppv = (form.ppv_folder ?? "").trim() && (form.ppv_price_cents ?? 0) > 0 ? 1 : 0;
  const messages = turns + 1 + ppv;
  const images = turns * per;
  // Outbound TURNS, which is what waits on him — one fewer than `messages` when the
  // apology carries the first gift, since those two bubbles go out together.
  const outbound = ((form.open_with_gift ?? true) && turns > 0 ? turns : turns + 1) + ppv;

  return (
    <div className="text-[11px] text-fg-dim bg-bg border border-border rounded-lg px-2.5 py-2 leading-relaxed">
      <b className="text-fg">
        {messages} message{messages === 1 ? "" : "s"} · {images} free image{images === 1 ? "" : "s"}
        {ppv ? " · 1 paid PPV" : ""}
      </b>
      <div className="mt-0.5">
        {outbound <= 1
          ? "One turn — it lands and closes itself. No reply needed, no nudge, nothing pending."
          : `Only the first lands right away; the other ${outbound - 1} wait for him to reply (nudge at ${form.nudge_hours ?? 24}h, close at ${form.close_hours ?? 48}h).`}
        {" "}A gift bubble is skipped when the tip library has nothing he hasn&apos;t
        already seen — <b>Preview</b> shows the pieces he&apos;d really get.
      </div>
    </div>
  );
}

const ACTION_LABEL: Record<string, string> = {
  would_open: "start exchange", in_progress: "exchange in progress",
  operator_only: "operator only", excluded: "skipped (guarded)",
  already_handled: "already handled", stale: "too late — thread moved on",
};

export default function MakeRightTab({ accountId }: { accountId: string | null }) {
  const { current: employee } = useEmployee();
  const ctx = { accountId: accountId ?? undefined, employeeId: employee?.id };

  const [form, setForm] = useState<Cfg>({});
  const [loaded, setLoaded] = useState(false);
  const [preview, setPreview] = useState<PreviewResp | null>(null);
  const [busy, setBusy] = useState<"" | "save" | "preview" | "run">("");
  const [error, setError] = useState<string | null>(null);
  const [okMsg, setOkMsg] = useState<string | null>(null);
  const [pickPpvFolder, setPickPpvFolder] = useState(false);

  useEffect(() => {
    if (!accountId) return;
    setLoaded(false); setPreview(null); setError(null); setOkMsg(null);
    relay.get<{ config: Cfg; defaults: Cfg }>(
      `/admin/make-right-config?account_id=${encodeURIComponent(accountId)}`, ctx)
      .then((r) => { setForm({ ...r.defaults, ...r.config }); setLoaded(true); })
      .catch((e) => { setError((e as Error)?.message || "Load failed"); setLoaded(true); });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [accountId]);

  const set = (patch: Partial<Cfg>) => setForm((f) => ({ ...f, ...patch }));
  const dollars = (c?: number) => Math.round(((c ?? 0) / 100) * 100) / 100;
  const cents = (d: number) => Math.round(d * 100);

  async function save() {
    if (!accountId) { setError("Pick an account first."); return; }
    setError(null); setOkMsg(null); setBusy("save");
    try {
      await relay.put("/admin/make-right-config", { account_id: accountId, config: form }, ctx);
      setOkMsg("Saved ✓");
    } catch (e) { setError((e as Error)?.message || "Save failed"); }
    finally { setBusy(""); }
  }

  async function runPreview() {
    if (!accountId) { setError("Pick an account first."); return; }
    setError(null); setOkMsg(null); setBusy("preview");
    try {
      setPreview(await relay.post<PreviewResp>(
        "/admin/make-right-preview", { account_id: accountId }, ctx));
    } catch (e) { setError((e as Error)?.message || "Preview failed"); }
    finally { setBusy(""); }
  }

  async function runNow() {
    if (!accountId) { setError("Pick an account first."); return; }
    setError(null); setOkMsg(null);
    if (!form.enabled || !form.auto_send) {
      setError("Turn on BOTH Enabled and Auto-send (and Save) before a real run — otherwise nothing sends.");
      return;
    }
    setBusy("run");
    let n = 0;
    try {
      const pv = await relay.post<PreviewResp>("/admin/make-right-preview", { account_id: accountId }, ctx);
      setPreview(pv); n = pv.would_open ?? 0;
    } catch (e) { setBusy(""); setError((e as Error)?.message || "Couldn't check incidents"); return; }
    if (n === 0) { setBusy(""); setOkMsg("No new make-rights to send right now — nothing queued."); return; }
    if (!window.confirm(
      `Send a real apology + free gift to ${n} fan(s) who were wrongly charged?\n\n` +
      `This sends real DMs and free content, and flags any refunds for you to action in OF.`)) {
      setBusy(""); return;
    }
    try {
      const resp = await relay.post<{ enqueued_job_id: number }>(
        "/admin/automation/enqueue",
        { account_id: accountId, kind: "make_right", payload: {} }, ctx);
      setOkMsg(`Queued (job #${resp.enqueued_job_id}) — sends within ~30s.`);
    } catch (e) { setError((e as Error)?.message || "Run failed"); }
    finally { setBusy(""); }
  }

  if (!accountId) return <div className="text-sm text-fg-dim">Pick an account above.</div>;
  if (!loaded) return <div className="text-sm text-fg-dim">Loading…</div>;

  const enabled = !!form.enabled;

  return (
    <div className="space-y-4">
      <div>
        <h3 className="text-sm font-medium text-fg">🤝 Make It Right (the safety net)</h3>
        <p className="text-xs text-fg-dim leading-relaxed mt-1">
          Catches when a fan got the wrong outcome — above all, <b>charged twice for the
          same content</b> — and makes him whole: <b>one apology message + one bubble of
          free, unseen</b> content from your tip library, then it stops. It fires
          <b> up to twice per fan</b>, then hands him to you. <b>Refunds are flagged for you</b>,
          never moved automatically. <b>On by default</b> — untick <b>Enabled</b> or
          <b> Auto-send</b> to opt this account out. <b>Preview</b> shows who&apos;d get what
          without sending.
        </p>
      </div>

      <Card className="p-4 space-y-4 max-w-2xl">
        <div className="flex flex-wrap items-center gap-5">
          <label className="flex items-center gap-2 cursor-pointer">
            <input type="checkbox" className="h-4 w-4 accent-[var(--accent)]"
              checked={enabled} onChange={(e) => set({ enabled: e.target.checked })} />
            <span className="text-sm">{enabled ? "Enabled" : "Disabled"}</span>
          </label>
          <label className="flex items-center gap-2 cursor-pointer">
            <input type="checkbox" className="h-4 w-4 accent-[var(--accent)]"
              checked={!!form.auto_send} onChange={(e) => set({ auto_send: e.target.checked })} />
            <span className="text-sm">Auto-send <span className="text-[11px] text-fg-dim/70">(off = preview only)</span></span>
          </label>
        </div>

        <fieldset className="space-y-4" style={{ opacity: enabled ? 1 : 0.55 }}>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
            <NumField label="Look back" hint="days" value={form.lookback_days ?? 30} min={1} max={365}
              onChange={(n) => set({ lookback_days: n })} suffix="d" />
            <NumField label="Only if fresh" hint="msgs since the charge (0 = any age)"
              value={form.max_msgs_since_charge ?? 3} min={0} max={1000}
              onChange={(n) => set({ max_msgs_since_charge: n })} suffix="msg" />
            <NumField label="Max per fan" hint="then operator" value={form.per_fan_cap ?? 2} min={1} max={10}
              onChange={(n) => set({ per_fan_cap: n })} />
          </div>
          {/* "Free pieces min/max", "Match the over-charge" and "Piece value" lived
              here. Four fields for one number the engine had stopped reading — v1
              solved a piece count from the over-charge, v2 takes it straight from
              "Images per turn" below. Gift size is that one field now; the other
              four are deleted everywhere, tab through validator. */}
          <div className="flex flex-wrap gap-4 items-end">
            <NumField label="Skip if messaged within" hint="contact-guard" value={form.guard_hours ?? 12} min={0} max={8760}
              onChange={(n) => set({ guard_hours: n })} suffix="h" />
            <label className="flex items-center gap-2 cursor-pointer pb-1.5">
              <input type="checkbox" className="h-4 w-4 accent-[var(--accent)]"
                checked={form.flag_refund ?? true}
                onChange={(e) => set({ flag_refund: e.target.checked })} />
              <span className="text-sm">Flag refunds for me</span>
            </label>
          </div>
          <label className="space-y-1 block max-w-md">
            <div className="text-xs text-fg-dim">Gift tier <span className="opacity-60">(a tip-reward tier name, blank = auto)</span></div>
            <Input value={form.gift_tier ?? ""} placeholder="e.g. basic"
              onChange={(e) => set({ gift_tier: e.target.value })} />
          </label>
          <label className="space-y-1 block max-w-md">
            <div className="text-xs text-fg-dim">Apology caption override <span className="opacity-60">(blank = warm auto text)</span></div>
            <Input value={form.apology_caption ?? ""} placeholder="on me, so sorry babe 💕"
              onChange={(e) => set({ apology_caption: e.target.value })} />
          </label>

          <div className="border-t border-border/60 pt-3 space-y-3">
            <div className="text-xs font-medium text-fg">The exchange <span className="text-fg-dim/70 font-normal">— apology → free replies (each waits for HIS reply) → a PPV → close</span></div>
            <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
              <NumField label="Free turns" hint="1 = apology + gift, then done" value={form.free_steps ?? 1} min={0} max={10}
                onChange={(n) => set({ free_steps: n })} />
              <NumField label="Images per turn" hint="all on one bubble" value={form.gift_pieces_per_step ?? 1} min={1} max={10}
                onChange={(n) => set({ gift_pieces_per_step: n })} />
              <NumField label="Nudge after" hint="silent hrs" value={form.nudge_hours ?? 24} min={1} max={8760}
                onChange={(n) => set({ nudge_hours: n })} suffix="h" />
              <NumField label="Close after" hint="silent hrs" value={form.close_hours ?? 48} min={1} max={8760}
                onChange={(n) => set({ close_hours: n })} suffix="h" />
            </div>
            <ExchangeShape form={form} />
            <label className="flex items-center gap-2 cursor-pointer">
              <input type="checkbox" className="h-4 w-4 accent-[var(--accent)]"
                checked={form.open_with_gift ?? true}
                onChange={(e) => set({ open_with_gift: e.target.checked })} />
              <span className="text-sm">Send the first free piece with the apology</span>
            </label>
            {/* The apology gift's CONTENT, not its size. Off by default: reading
                the thread for what he asked costs an LLM call per make-right,
                and the tip shelf is the safe fallback when nothing matches. */}
            <label className="flex items-start gap-2 cursor-pointer">
              <input type="checkbox" className="mt-0.5 h-4 w-4 accent-[var(--accent)]"
                checked={form.gift_on_subject ?? false}
                onChange={(e) => set({ gift_on_subject: e.target.checked })} />
              <span className="text-sm">
                Make the free piece the thing he asked for
                <span className="block text-fg-dim text-xs">
                  Reads what he was actually after and picks the gift to match, instead
                  of the next unseen piece off the tip shelf. If nothing in the vault
                  fits, it falls back to the shelf rather than sending something wrong —
                  an apology that hands him the wrong thing again is a second mistake,
                  not a fix.
                </span>
              </span>
            </label>
            <div className="flex flex-wrap gap-4 items-end">
              <div className="space-y-1">
                <div className="text-xs text-fg-dim">PPV folder <span className="opacity-60">(vault folder; none = no PPV, just close)</span></div>
                <SingleFolderRow folder={form.ppv_folder ?? ""}
                  onPick={() => setPickPpvFolder(true)}
                  onClear={() => set({ ppv_folder: "" })} />
              </div>
              <NumField label="PPV price" value={dollars(form.ppv_price_cents ?? 1500)} min={0} max={10000} step={0.01} suffix="$"
                onChange={(n) => set({ ppv_price_cents: cents(n) })} />
            </div>
            <label className="space-y-1 block max-w-md">
              <div className="text-xs text-fg-dim">PPV tease caption <span className="opacity-60">(blank = built-in)</span></div>
              <Input value={form.ppv_caption ?? ""} placeholder="now that we're good 😌 unlock this? 😏"
                onChange={(e) => set({ ppv_caption: e.target.value })} />
            </label>
          </div>
        </fieldset>

        <div className="flex items-center gap-2 border-t border-border pt-3 flex-wrap md:flex-nowrap">
          <Button className="shrink-0" size="sm" variant="secondary" disabled={busy !== ""} onClick={save}>
            {busy === "save" ? "Saving…" : "Save"}
          </Button>
          <Button className="shrink-0" size="sm" variant="secondary" disabled={busy !== ""} onClick={runPreview}>
            {busy === "preview" ? "Checking…" : "Preview (dry run)"}
          </Button>
          <Button className="shrink-0" size="sm" variant="primary" disabled={busy !== ""} onClick={runNow}>
            {busy === "run" ? "Queuing…" : "Run now"}
          </Button>
          {error && <span className="text-err text-[11px] basis-full md:basis-auto">{error}</span>}
          {okMsg && <span className="text-ok text-[11px] basis-full md:basis-auto">{okMsg}</span>}
        </div>
      </Card>

      {preview && (
        <Card className="p-4 space-y-2">
          <div className="text-sm">
            {preview.candidates === 0 ? (
              <span className="text-fg-dim">No wrong-content incidents found in the last {form.lookback_days ?? 30} days. 🎉</span>
            ) : (
              <span className="text-fg">
                <b>{preview.candidates}</b> incident(s): <b>{preview.would_open ?? 0}</b> would start an exchange,
                {" "}<b>{preview.operator_only ?? 0}</b> operator-only,
                {" "}{preview.in_progress ?? 0} already in progress,
                {" "}{preview.excluded ?? 0} skipped, {preview.already_handled ?? 0} already handled,
                {" "}<b>{preview.stale ?? 0}</b> too late (thread moved on).
                {preview.preview_only_reason && (
                  <span className="text-fg-dim"> (preview-only: {preview.preview_only_reason})</span>
                )}
              </span>
            )}
          </div>
          {(preview.preview ?? []).length > 0 && (
            <ul className="space-y-2">
              {preview.preview.map((r) => (
                <li key={r.incident_key} className="text-sm border border-border rounded-lg p-2.5">
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="text-fg-dim text-xs">{r.name || r.fan_id}</span>
                    <span className="text-[10px] uppercase tracking-wide bg-accent/10 border border-accent/30 rounded px-1.5 py-0.5">
                      {ACTION_LABEL[r.action] || r.action}{r.reason ? ` · ${r.reason}` : ""}
                    </span>
                    {typeof r.wrongful_cents === "number" && r.wrongful_cents > 0 && (
                      <span className="text-[11px] text-fg-dim">over-charged ${(r.wrongful_cents / 100).toFixed(2)}</span>
                    )}
                    {r.refund_flagged && <span className="text-[11px] text-amber-500">refund flagged</span>}
                    {r.gift_media_ids && r.gift_media_ids.length > 0 && (
                      <span className="text-[11px] text-fg-dim">gift ×{r.gift_media_ids.length}</span>
                    )}
                  </div>
                  {r.apology && (
                    <div className="mt-1.5 bg-accent/10 border border-accent/30 rounded-2xl px-3 py-1 inline-block whitespace-pre-line">
                      {r.apology}
                    </div>
                  )}
                  {r.steps && r.steps.length > 0 && (
                    <div className="mt-1 text-[11px] text-fg-dim/70">
                      exchange: {r.steps.map((s) => s.replace("apology_gift", "apology+gift")).join(" → ")}
                      {typeof r.step === "number" && r.step > 0 ? ` (at step ${r.step})` : ""}
                    </div>
                  )}
                </li>
              ))}
            </ul>
          )}
        </Card>
      )}

      {/* Single-folder vault picker for the PPV pivot (same idiom as the
          hot-teaser branches in TipRewardTab — one folder, take the first). */}
      {pickPpvFolder && (
        <VaultFolderPicker
          open
          accountId={accountId}
          initialSelected={form.ppv_folder ? [form.ppv_folder] : []}
          onClose={() => setPickPpvFolder(false)}
          onConfirm={(folders) => set({ ppv_folder: folders[0] ?? "" })}
        />
      )}
    </div>
  );
}
