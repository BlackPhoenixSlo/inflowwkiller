"use client";

/**
 * useSettingsTransfer — full account-settings export/import.
 *
 *   GET  /admin/settings-export?account_id=  → { document }
 *   POST /admin/settings-import              → { mode, applied, report, backup_document, backup_id }
 *
 * The import flow ALWAYS downloads the target's current settings as a JSON
 * file before POSTing (the server additionally persists the same preimage to
 * disk and echoes it as `backup_document` — the download here is best-effort
 * initiation; the server file is the guaranteed layer).
 */

import { useMutation, useQueryClient } from "@tanstack/react-query";

import { relay, type RelayContext } from "@/lib/relay";

const BG_CTX: RelayContext = { priority: "user" };

export interface SettingsDocument {
  format: string;
  version: number;
  exported_at?: string;
  account_id: string;
  nickname?: string | null;
  funnel_names?: Record<string, string>;
  sections: Record<string, unknown>;
}

export interface ImportRuleEntry {
  source_ordinal: number | null;
  kind: string;
  name: string;
  match_method: string | null;
  target_rule_id: number | null;
  source_enabled: boolean;
  final_enabled: boolean | null;
  remediation: "relink_media" | "repick_audience" | "safe" | "review";
}

export interface ImportReport {
  sections_applied: string[];
  rules: {
    imported: number;
    updated: number;
    disabled_missing: number;
    skipped_ppv_send: number;
    unknown_disabled: number;
    entries: ImportRuleEntry[];
  };
  config_flags_forced_off: Record<string, unknown>;
  created_rows: Record<string, number[]>;
  surplus_rows: Array<{
    entity: string;
    target_id: number;
    identifying_fields: Record<string, unknown>;
    reason: string;
  }>;
  deleted_rows: Array<Record<string, unknown>>;
  media_stripped: string[];
  identity_stripped: string[];
  backstop_hits: string[];
  warnings: string[];
}

export interface ImportResponse {
  mode: "restore" | "clone";
  applied: boolean;
  report: ImportReport;
  backup_document: SettingsDocument;
  backup_id: string;
}

/** Trigger a client-side download of `obj` as pretty-printed JSON. Browsers
 *  expose no completion signal for anchor downloads — this INITIATES one. */
export function downloadJson(obj: unknown, filename: string): void {
  const blob = new Blob([JSON.stringify(obj, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
}

export function fetchSettingsExport(accountId: string): Promise<SettingsDocument> {
  return relay
    .get<{ document: SettingsDocument }>(
      `/admin/settings-export?account_id=${encodeURIComponent(accountId)}`,
      BG_CTX,
    )
    .then((r) => r.document);
}

/** Every settings surface the import can touch — invalidated together on
 *  success. Prefix keys (["ai-chatter"], ["funnel-media"]) partial-match all
 *  their sub-keys. NOTE the ai_chatter key is "ai-chatter", NOT the surface
 *  label "ai-chatter-config". */
function settingsQueryKeys(accountId: string): unknown[][] {
  return [
    ["account-config", accountId],
    ["ppv-library-config", accountId],
    ["nudge-config", accountId],
    ["webhook-config", accountId],
    ["style-config", accountId],
    ["autoreply-config", accountId],
    ["tip-reward-config", accountId],
    ["ai-chatter"],
    ["automation-rules", accountId],
    ["automation-kinds"],
    ["funnel-media"],
    ["saved-replies", accountId],
    ["raw-json-config"],
  ];
}

export function useSettingsImport(accountId: string | null) {
  const qc = useQueryClient();
  return useMutation<ImportResponse, Error, SettingsDocument>({
    mutationFn: (document) =>
      relay.post<ImportResponse>(
        "/admin/settings-import",
        { account_id: accountId, document },
        BG_CTX,
      ),
    onSettled: () => {
      // Invalidate even on unknown/network errors: a timeout can land AFTER
      // the server committed — stale tabs would misreport the account state.
      if (!accountId) return;
      for (const key of settingsQueryKeys(accountId)) {
        qc.invalidateQueries({ queryKey: key });
      }
    },
  });
}
