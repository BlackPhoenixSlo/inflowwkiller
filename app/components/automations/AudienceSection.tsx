"use client";

/**
 * AudienceSection — the Brain's include-only automation audience controls
 * (mode off/shadow/enforce, folder picker, auto-add, backfill, status line,
 * exemption register). Extracted from BrainPanel: the section is self-contained
 * and that panel is already well past its healthy size.
 *
 * State contract: the three audience fields live on the Brain FORM (they must
 * ride the whole-object PUT or a Save would drop them — see BrainConfig), so
 * this component owns no form state; it reports edits up via `onChange` and
 * the parent's Save button persists them.
 */

import { Button } from "@/components/ui/primitives";
import FanListSelect from "@/components/automations/FanListSelect";
import type { AudienceStatus, BrainConfig } from "@/hooks/useAccountConfig";
import { relay } from "@/lib/relay";
import { useState } from "react";
import type { ChatFolder } from "@/hooks/useChatFolders";

export default function AudienceSection({
  accountId,
  mode,
  ofListId,
  autoAdd,
  status,
  selectCls,
  onChange,
  onMsg,
  refetchConfig,
}: {
  accountId: string | null;
  mode: string;
  ofListId: number | null;
  autoAdd: boolean;
  status: AudienceStatus | undefined;
  selectCls: string;
  onChange: (patch: Partial<BrainConfig>) => void;
  onMsg: (m: string) => void;
  refetchConfig: () => void;
}) {
  const [folder, setFolder] = useState<ChatFolder | null>(null);
  // Unknown folder (stored selection not re-picked this session) → leave the
  // checkbox usable; the server re-validates writability on save/sync.
  const folderCanAdd = folder ? !!folder.canAddUsers : true;

  function onModeChange(v: string) {
    if (v === "enforce" && status && status.mode !== "shadow" && status.mode !== "enforce") {
      // Shadow is mandatory before enforce; direct-to-enforce is an explicit,
      // logged override — the confirm shows what we know right now.
      const ok = window.confirm(
        "Enforce without a shadow week?\n\n" +
        `Folder members: ${status.member_count}. Known active subs outside the ` +
        `fence: ${status.outside_queue ?? 0} — they would stop getting automated ` +
        "messages.\n\nRecommended: run Shadow first and read the skip counts. " +
        "OK = enforce anyway (logged as an explicit override).",
      );
      if (!ok) return;
      onChange({ audience_mode: "enforce", audience_enforce_override: true });
      return;
    }
    onChange({ audience_mode: v, audience_enforce_override: undefined });
  }

  async function backfill() {
    if (!accountId) return;
    try {
      const preview = await relay.post<{ would_admit: number }>(
        "/admin/audience/backfill", { account_id: accountId, confirm: false });
      const n = preview.would_admit ?? 0;
      if (n === 0) {
        onMsg("Backfill: nobody is waiting outside the fence.");
        return;
      }
      if (!window.confirm(
        `Add ${n} existing fan(s) from the outside-the-fence queue to the ` +
        "folder? (Operator-removed fans are final and are never re-added.)")) {
        return;
      }
      const res = await relay.post<{ admitted: number }>(
        "/admin/audience/backfill", { account_id: accountId, confirm: true });
      onMsg(`Backfill queued: ${res.admitted} fan(s) will be added on the next sync.`);
      refetchConfig();
    } catch (e) {
      onMsg(`Backfill failed: ${e instanceof Error ? e.message : String(e)}`);
    }
  }

  return (
    <div className="space-y-2 border-t border-border pt-3">
      <span className="text-[11px] uppercase tracking-wide text-fg-dim">Audience</span>
      <p className="text-[11px] text-fg-dim">
        Run automations on fans in one folder. Off = everyone (today&apos;s
        behavior). Shadow = nothing changes, but every would-be skip is counted
        so the cost is readable before you enforce. Enforce = gated automations
        act on folder members; broadcasts keep their audience and subtract
        everyone else via the AUTOFENCE list.
      </p>
      <div className="flex flex-wrap items-end gap-3">
        <label className="block space-y-1">
          <span className="text-[11px] text-fg-dim">Mode</span>
          <select
            value={mode}
            onChange={(e) => onModeChange(e.target.value)}
            className={selectCls}
          >
            <option value="off">Off — all fans</option>
            <option value="shadow">Shadow — measure only</option>
            <option value="enforce">Enforce — folder members</option>
          </select>
        </label>
        <label className="block space-y-1 min-w-[220px]">
          <span className="text-[11px] text-fg-dim">Folder</span>
          <FanListSelect
            accountId={accountId}
            value={ofListId}
            onChange={(id, f) => {
              setFolder(f);
              onChange({
                audience_of_list_id: id,
                ...(f && !f.canAddUsers ? { audience_auto_add: false } : {}),
              });
            }}
            className={selectCls}
          />
        </label>
        <label className="flex items-center gap-2 pb-1.5 text-[12px] text-fg">
          <input
            type="checkbox"
            checked={autoAdd}
            disabled={!folderCanAdd}
            onChange={(e) => onChange({ audience_auto_add: e.target.checked })}
          />
          auto-add new subscribers
          {!folderCanAdd && (
            <span className="text-fg-dim">(folder is read-only on OF)</span>
          )}
        </label>
        <Button size="sm" variant="ghost" onClick={backfill}
          disabled={!accountId || mode === "off"}>
          Backfill existing fans…
        </Button>
      </div>
      {status && status.mode !== "off" && (
        <p className="text-[11px] text-fg-dim">
          {(status.list_name ?? "folder")}: {status.member_count} member(s)
          {status.synced_at
            ? ` · synced ${Math.max(0, Math.round((status.snapshot_age_s ?? 0) / 60))}m ago`
            : " · never synced"}
          {status.pending_first_sync ? " · first sync pending — enforce sits in shadow until it lands" : ""}
          {status.truncated ? " · ⚠ this list exceeds the 20k sync bound; enforcement unavailable" : ""}
          {status.paused ? " · ⏸ PAUSED by the kill switch (paused, not unscoped)" : ""}
          {!status.healthy && !status.pending_first_sync && status.reason
            ? ` · ⚠ ${status.reason}` : ""}
          {typeof status.outside_queue === "number"
            ? ` · ${status.outside_queue} fan(s) in the outside-the-fence queue` : ""}
        </p>
      )}
      <p className="text-[11px] text-fg-dim">
        Exemptions — these still reach fans regardless of the folder:
        manual/chatter sends (including force-targeted runs), tip-reward
        acknowledgments, make-right repair messages, unsend cleanup, profile
        posts &amp; stories, and OF&apos;s own native welcome message.
        &quot;Only this folder&quot; always means: only, except this list.
      </p>
    </div>
  );
}
