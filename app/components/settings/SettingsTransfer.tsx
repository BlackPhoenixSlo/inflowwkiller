"use client";

/**
 * SettingsTransfer — Export / Import the FULL per-account settings as JSON.
 *
 * Export: downloads `settings-{nickname}-{id}-{date}.json` (all 7 sections).
 * Import: file picker → confirm dialog (restore vs CLONE banner) → the
 * target's CURRENT settings are downloaded as a backup file FIRST → POST →
 * report modal. On clone, media + audience/identity references are excluded
 * and EVERYTHING imports switched off — the report is the re-enable worksheet.
 *
 * The account id is SNAPSHOTTED when the dialog opens; if the panel's active
 * account changes mid-flow the dialog cancels itself (no cross-account import).
 */

import { useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/primitives";
import {
  downloadJson,
  fetchSettingsExport,
  useSettingsImport,
  type ImportResponse,
  type SettingsDocument,
} from "@/hooks/useSettingsTransfer";

function today(): string {
  return new Date().toISOString().slice(0, 10);
}

function fileSlug(nickname: string | null | undefined, id: string): string {
  const nick = (nickname || "").replace(/[^a-z0-9-]+/gi, "").toLowerCase();
  return nick ? `${nick}-${id}` : id;
}

const SECTION_LABELS: Record<string, string> = {
  account_ai_config: "Brain + all config tabs",
  automation_rules: "Automation rules",
  funnel_account_media: "Funnel media",
  catalog_scripts: "AI Seller scripts",
  catalog_items: "AI Seller catalog",
  saved_replies: "Saved replies",
  prompts: "Prompts",
};

export default function SettingsTransfer({
  accountId,
  nickname,
}: {
  accountId: string | null;
  nickname?: string | null;
}) {
  const fileRef = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState<"export" | "import" | null>(null);
  const [err, setErr] = useState<string | null>(null);
  // Pending import: parsed document + the account snapshot taken at file pick.
  const [pending, setPending] = useState<{ doc: SettingsDocument; target: string } | null>(null);
  const [result, setResult] = useState<ImportResponse | null>(null);
  const importM = useSettingsImport(pending?.target ?? accountId);

  // Target drift guard: active account changed while the dialog was open.
  useEffect(() => {
    if (pending && accountId !== pending.target) {
      setPending(null);
      setErr("Active account changed — import cancelled.");
    }
  }, [accountId, pending]);

  async function doExport() {
    if (!accountId) return;
    setErr(null);
    setBusy("export");
    try {
      const doc = await fetchSettingsExport(accountId);
      downloadJson(doc, `settings-${fileSlug(doc.nickname, accountId)}-${today()}.json`);
    } catch (e) {
      setErr((e as Error)?.message || "Export failed");
    } finally {
      setBusy(null);
    }
  }

  function onPickFile(ev: React.ChangeEvent<HTMLInputElement>) {
    setErr(null);
    const file = ev.target.files?.[0];
    // Always reset so re-selecting the same file re-triggers `change`.
    ev.target.value = "";
    if (!file || !accountId) return;
    if (file.size > 5 * 1024 * 1024) {
      setErr("File too large (max 5 MB).");
      return;
    }
    file
      .text()
      .then((text) => {
        let doc: SettingsDocument;
        try {
          doc = JSON.parse(text);
        } catch {
          setErr("Not valid JSON.");
          return;
        }
        if (doc?.format !== "chatterly-settings" || typeof doc?.version !== "number") {
          setErr("Not a Fastt settings export.");
          return;
        }
        if (doc.version !== 1) {
          setErr(`Unsupported export version ${doc.version} (supported: 1).`);
          return;
        }
        if (!doc.sections || !doc.account_id) {
          setErr("File is missing sections / account id — looks truncated.");
          return;
        }
        setPending({ doc, target: accountId });
      })
      .catch(() => setErr("Could not read the file."));
  }

  async function doImport() {
    if (!pending) return;
    const { doc, target } = pending;
    setErr(null);
    setBusy("import");
    try {
      // Backup FIRST: download the target's current settings before POSTing.
      // (Browser downloads are initiation-only; the server persists the same
      // preimage to disk and echoes it in the response as backup_document.)
      const current = await fetchSettingsExport(target);
      downloadJson(current, `backup-${fileSlug(current.nickname, target)}-${today()}.json`);
      const resp = await importM.mutateAsync(doc);
      setPending(null);
      setResult(resp);
    } catch (e) {
      setErr((e as Error)?.message ||
        "Import failed — it MAY still have applied; refresh and check before retrying.");
    } finally {
      setBusy(null);
    }
  }

  const isClone = pending ? pending.doc.account_id !== pending.target : false;

  return (
    <>
      <Button size="sm" variant="secondary" onClick={doExport} disabled={!accountId || busy !== null}>
        {busy === "export" ? "Exporting…" : "⬇ Export settings"}
      </Button>
      <Button
        size="sm"
        variant="secondary"
        onClick={() => fileRef.current?.click()}
        disabled={!accountId || busy !== null}
      >
        ⬆ Import settings
      </Button>
      <input ref={fileRef} type="file" accept=".json,application/json" className="hidden" onChange={onPickFile} />
      {err && <span className="text-xs text-red-400">{err}</span>}

      {/* ── Confirm dialog ─────────────────────────────────────────────── */}
      {pending && (
        <div className="fixed inset-0 z-50 bg-black/50 grid place-items-center p-4">
          <div className="bg-bg-elev-1 border border-border rounded-lg max-w-lg w-full p-4 space-y-3">
            <h3 className="text-sm font-medium text-fg">Import settings</h3>
            <p className="text-xs text-fg-dim">
              Source: <span className="text-fg">{pending.doc.nickname || pending.doc.account_id}</span>
              {" "}({pending.doc.account_id}) → Target:{" "}
              <span className="text-fg">{nickname || pending.target}</span> ({pending.target})
            </p>
            {isClone ? (
              <div className="text-xs rounded border border-amber-500/50 bg-amber-500/10 text-amber-300 p-2">
                <b>CLONE</b> — media AND audience/identity references are excluded, and
                EVERYTHING imports switched OFF. Review the report, re-link media and
                audiences, then re-enable each automation deliberately.
              </div>
            ) : (
              <div className="text-xs rounded border border-emerald-500/50 bg-emerald-500/10 text-emerald-300 p-2">
                <b>RESTORE</b> — same account: everything is applied verbatim, media included.
              </div>
            )}
            <ul className="text-xs text-fg-dim list-disc pl-4">
              {Object.keys(pending.doc.sections).map((k) => (
                <li key={k}>{SECTION_LABELS[k] || k}</li>
              ))}
            </ul>
            <p className="text-[11px] text-fg-dim">
              A backup of this account&apos;s CURRENT settings downloads first (the server
              also keeps a copy on disk).
            </p>
            <div className="flex justify-end gap-2">
              <Button size="sm" variant="secondary" onClick={() => setPending(null)} disabled={busy !== null}>
                Cancel
              </Button>
              <Button size="sm" variant="primary" onClick={doImport} disabled={busy !== null}>
                {busy === "import" ? "Importing…" : "Backup + import"}
              </Button>
            </div>
          </div>
        </div>
      )}

      {/* ── Report modal ───────────────────────────────────────────────── */}
      {result && (
        <div className="fixed inset-0 z-50 bg-black/50 grid place-items-center p-4">
          <div className="bg-bg-elev-1 border border-border rounded-lg max-w-2xl w-full p-4 space-y-3 max-h-[85vh] overflow-y-auto">
            <h3 className="text-sm font-medium text-fg">
              Import applied — {result.mode.toUpperCase()}
              <span className="ml-2 text-xs text-fg-dim">backup: {result.backup_id}</span>
            </h3>

            {result.report.backstop_hits.length > 0 && (
              <div className="text-xs rounded border border-red-500/60 bg-red-500/10 text-red-300 p-2">
                <b>⚠ BACKSTOP ALARM</b> — references were caught OUTSIDE the known strip
                spec (a spec gap; report this): {result.report.backstop_hits.join(", ")}
              </div>
            )}

            <p className="text-xs text-fg-dim">
              Sections: {result.report.sections_applied.join(", ")} · Rules:{" "}
              {result.report.rules.updated} updated, {result.report.rules.imported} added,{" "}
              {result.report.rules.disabled_missing} disabled (not in file),{" "}
              {result.report.rules.skipped_ppv_send} PPV rules regenerated,{" "}
              {result.report.rules.unknown_disabled} unknown kinds disabled.
            </p>

            {Object.keys(result.report.config_flags_forced_off).length > 0 && (
              <div className="text-xs text-fg-dim">
                <b className="text-fg">Config switches turned OFF</b> (were on at source):{" "}
                {Object.keys(result.report.config_flags_forced_off).join(", ")}
              </div>
            )}

            {result.report.rules.entries.length > 0 && (
              <div className="text-xs">
                <b className="text-fg">Rules review list</b>
                <table className="w-full mt-1 text-left text-fg-dim">
                  <thead>
                    <tr className="text-[10px] uppercase tracking-wide">
                      <th className="pr-2">Rule</th>
                      <th className="pr-2">Match</th>
                      <th className="pr-2">Was on</th>
                      <th className="pr-2">Now</th>
                      <th>Next step</th>
                    </tr>
                  </thead>
                  <tbody>
                    {result.report.rules.entries.map((e, i) => (
                      <tr key={i} className="border-t border-border/50">
                        <td className="pr-2 py-0.5 text-fg">{e.name}</td>
                        <td className="pr-2">{e.match_method}</td>
                        <td className="pr-2">{e.source_enabled ? "on" : "off"}</td>
                        <td className="pr-2">{e.final_enabled ? "on" : "off"}</td>
                        <td>
                          {e.remediation === "relink_media" && "re-link media"}
                          {e.remediation === "repick_audience" && "re-pick audience"}
                          {e.remediation === "review" && "review"}
                          {e.remediation === "safe" && "—"}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}

            {Object.values(result.report.created_rows).some((ids) => ids.length > 0) && (
              <p className="text-xs text-fg-dim">
                <b className="text-fg">Created</b>:{" "}
                {Object.entries(result.report.created_rows)
                  .filter(([, ids]) => ids.length > 0)
                  .map(([ent, ids]) => `${ids.length} ${ent.replace(/_/g, " ")}`)
                  .join(", ")}
              </p>
            )}

            {result.report.surplus_rows.length > 0 && (
              <div className="text-xs rounded border border-amber-500/50 bg-amber-500/10 text-amber-300 p-2">
                <b>Manual cleanup required</b> — rows on this account that are NOT in the
                imported file (they stay active and can influence AI behavior):
                <ul className="list-disc pl-4 mt-1">
                  {result.report.surplus_rows.map((r, i) => (
                    <li key={i}>
                      {r.entity} #{r.target_id} {JSON.stringify(r.identifying_fields)}
                    </li>
                  ))}
                </ul>
              </div>
            )}

            {result.report.deleted_rows.length > 0 && (
              <p className="text-xs text-fg-dim">
                Removed (full-surface restore): {result.report.deleted_rows.length} funnel-media row(s).
              </p>
            )}

            {(result.report.media_stripped.length > 0 || result.report.identity_stripped.length > 0) && (
              <details className="text-xs text-fg-dim">
                <summary className="cursor-pointer text-fg">
                  Stripped references ({result.report.media_stripped.length} media,{" "}
                  {result.report.identity_stripped.length} audience/identity)
                </summary>
                <ul className="list-disc pl-4 mt-1 break-all">
                  {[...result.report.media_stripped, ...result.report.identity_stripped].map((p, i) => (
                    <li key={i}>{p}</li>
                  ))}
                </ul>
              </details>
            )}

            {result.report.warnings.length > 0 && (
              <ul className="text-xs text-amber-300 list-disc pl-4">
                {result.report.warnings.map((w, i) => (
                  <li key={i}>{w}</li>
                ))}
              </ul>
            )}

            <div className="flex justify-end">
              <Button size="sm" variant="primary" onClick={() => setResult(null)}>
                Done
              </Button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
