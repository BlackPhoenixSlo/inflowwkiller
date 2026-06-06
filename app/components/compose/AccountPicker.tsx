"use client";

/**
 * AccountPicker — a small dropdown the New-post / New-mass-message
 * modals use to choose which model account to send AS. Defers to the
 * current ScopeContext when scope is a single account; otherwise lets
 * the user pick from the active-sessions list.
 *
 * Lives under /compose/ rather than the chat sidebar because both
 * composers reuse it and neither is "chat" exactly.
 */

import { useEffect, useMemo } from "react";

import { useActiveAccounts } from "@/hooks/useAccounts";
import { useAllModelsInclude } from "@/hooks/useAllModelsInclude";
import { useScope } from "@/contexts/ScopeContext";

export function AccountPicker({
  value, onChange,
}: { value: string | null; onChange: (id: string) => void }) {
  const accounts = useActiveAccounts();
  const { isIncluded } = useAllModelsInclude();
  const { scope } = useScope();

  // Filter the sender list by the All-Models include set — models the
  // user opted out of the aggregate on the left sidebar shouldn't show
  // up as a "Post as:" option either. Pre-existing `value` is preserved
  // so a saved draft pointing at an excluded model still renders.
  const visibleAccounts = useMemo(
    () => accounts.filter((a) => isIncluded(a.id) || a.id === value),
    [accounts, isIncluded, value],
  );

  // Auto-seed the first time. In single-account scope this just sticks
  // to that account; in unified scope we pick the first session-bearing
  // model so the modal isn't blocked by an empty selection.
  useEffect(() => {
    if (value) return;
    const seed = scope.kind === "model" ? scope.accountId : visibleAccounts[0]?.id;
    if (seed) onChange(seed);
  }, [value, scope, visibleAccounts, onChange]);

  return (
    <div className="flex items-center gap-2">
      <label className="text-xs text-fg-dim">Post as:</label>
      <select
        value={value ?? ""}
        onChange={(e) => onChange(e.target.value)}
        className="flex-1 bg-bg border border-border rounded-md px-2 py-1.5 text-xs focus:outline-none focus:border-accent"
      >
        {visibleAccounts.length === 0 && <option value="">(no active sessions)</option>}
        {visibleAccounts.map((a) => (
          <option key={a.id} value={a.id}>
            {a.nickname || a.id}
          </option>
        ))}
      </select>
    </div>
  );
}
