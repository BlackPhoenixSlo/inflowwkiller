"use client";

/**
 * GroupMassButton — retro-tags OF-native mass blasts that the WS pump stored
 * as individual per-fan rows (mass_run_id NULL), so the list collapses them
 * into one "MASS · sent to N" summary instead of N identical "from us" rows.
 *
 * POSTs /admin/messages/detect-mass (scoped to the current model filter, or
 * every owned account when "all models" is selected), then invalidates the
 * paid-messages cache so the freshly-collapsed rows appear. Idempotent — the
 * sweep only touches rows not already tied to a run, so re-clicking is safe.
 */

import { useState } from "react";
import { Megaphone } from "lucide-react";
import { useQueryClient } from "@tanstack/react-query";

import { Button } from "@/components/ui/primitives";
import { relay } from "@/lib/relay";

interface DetectResult {
  runs_created: number;
  rows_tagged: number;
}

interface Props {
  /** Current model filter; null = every account the principal owns. */
  accountId?: string | null;
  disabled?: boolean;
}

export default function GroupMassButton({ accountId, disabled }: Props) {
  const qc = useQueryClient();
  const [busy, setBusy] = useState(false);

  const onClick = async () => {
    if (busy) return;
    setBusy(true);
    try {
      const qs = accountId ? `?account_id=${encodeURIComponent(accountId)}` : "";
      const res = await relay.post<DetectResult>(`/admin/messages/detect-mass${qs}`);
      await qc.invalidateQueries({ queryKey: ["paid-messages"] });
      const n = res?.runs_created ?? 0;
      if (typeof window !== "undefined") {
        window.alert(
          n > 0
            ? `Grouped ${n} mass blast${n === 1 ? "" : "s"} (${res.rows_tagged} sends).`
            : "No new mass blasts to group.",
        );
      }
    } catch (e) {
      if (typeof window !== "undefined") {
        window.alert(`Couldn't group mass sends: ${(e as Error)?.message || "unknown error"}`);
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <Button
      type="button"
      variant="secondary"
      size="sm"
      onClick={onClick}
      disabled={disabled || busy}
      title="Collapse OF-native mass blasts (stored as per-fan rows) into one MASS row each"
    >
      <Megaphone size={14} className="inline-block mr-1" aria-hidden />
      {busy ? "Grouping…" : "Group mass sends"}
    </Button>
  );
}
