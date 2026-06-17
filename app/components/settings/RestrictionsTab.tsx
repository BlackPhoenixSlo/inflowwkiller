"use client";

/**
 * RestrictionsTab — manage the fans a human has restricted from automations.
 *
 * "Restrict from automations" (the chat ⋯ menu) writes a durable skip_list row
 * (reason='manual_restrict') in our DB, so NO automation — welcome, AI chat,
 * follow-up, nudge, tip reward, mass blast — ever messages that fan again until
 * it's lifted. This tab lists those fans, lets you unrestrict one or all, and is
 * the Settings-side home of the same control.
 *
 * NOTE: peer-creators whose chat you MUTED on OF are auto-restricted
 * (reason='muted_creator') and are NOT listed here — un-mute their chat to free
 * them. This tab is the MANUAL list only.
 */

import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Ban, Trash2 } from "lucide-react";

import { Button, Card } from "@/components/ui/primitives";
import { useActiveAccounts } from "@/hooks/useAccounts";
import { relay } from "@/lib/relay";

const SELECT_CLS =
  "w-full bg-input border border-border rounded-md px-3 py-2 text-sm";

interface RestrictRow {
  fan_id: number;
  of_username: string | null;
  of_display_name: string | null;
  added_at: string | null;
}

export default function RestrictionsTab() {
  const accounts = useActiveAccounts();
  const [accountId, setAccountId] = useState<string | null>(null);
  useEffect(() => {
    if (!accountId && accounts.length > 0) setAccountId(accounts[0].id);
  }, [accounts, accountId]);

  const qc = useQueryClient();
  const listQ = useQuery({
    queryKey: ["automation-restrict-list", accountId],
    queryFn: () =>
      relay.get<{ count: number; list: RestrictRow[] }>(
        "/api/of/v2/automation-restrict", { accountId: accountId! }),
    enabled: !!accountId,
  });

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["automation-restrict-list", accountId] });
    qc.invalidateQueries({ queryKey: ["automation-restrict"] });
  };

  const unrestrictOne = useMutation({
    mutationFn: (fanId: number) =>
      relay.delete(`/api/of/v2/automation-restrict/${fanId}`, { accountId: accountId! }),
    onSuccess: invalidate,
  });
  const unrestrictAll = useMutation({
    mutationFn: () =>
      relay.delete("/api/of/v2/automation-restrict", { accountId: accountId! }),
    onSuccess: invalidate,
  });

  const rows = listQ.data?.list ?? [];

  return (
    <div className="space-y-4">
      <header className="flex items-start justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold flex items-center gap-2">
            <Ban size={18} /> Restricted from automations
          </h2>
          <p className="text-sm text-fg-dim mt-1 max-w-2xl">
            Fans here are excluded from <em>every</em> automation until you
            unrestrict them. Restrict a fan from their chat&apos;s ⋯ menu
            (&ldquo;Restrict from automations&rdquo;). Peer-creators whose chat
            you muted are auto-restricted and managed by un-muting — they
            don&apos;t appear in this list.
          </p>
        </div>
      </header>

      {accounts.length > 1 && (
        <label className="block max-w-xs">
          <span className="text-xs text-fg-dim">Account</span>
          <select
            className={SELECT_CLS}
            value={accountId ?? ""}
            onChange={(e) => setAccountId(e.target.value)}
          >
            {accounts.map((a) => (
              <option key={a.id} value={a.id}>{a.nickname || a.id}</option>
            ))}
          </select>
        </label>
      )}

      <Card className="p-4 space-y-3">
        <div className="flex items-center justify-between">
          <span className="text-sm text-fg-dim">
            {listQ.isLoading
              ? "Loading…"
              : `${rows.length} fan${rows.length === 1 ? "" : "s"} restricted`}
          </span>
          <Button
            variant="ghost"
            disabled={!rows.length || unrestrictAll.isPending}
            onClick={() => {
              if (confirm(`Unrestrict all ${rows.length} fan(s)? Automations will resume for them.`))
                unrestrictAll.mutate();
            }}
          >
            Unrestrict all
          </Button>
        </div>

        {!listQ.isLoading && rows.length === 0 && (
          <div className="text-sm text-fg-dim py-6 text-center">
            No fans are manually restricted.
          </div>
        )}

        {rows.length > 0 && (
          <ul className="divide-y divide-border">
            {rows.map((r) => (
              <li key={r.fan_id} className="flex items-center justify-between py-2">
                <div className="min-w-0">
                  <div className="text-sm truncate">
                    {r.of_display_name || r.of_username || `Fan ${r.fan_id}`}
                  </div>
                  <div className="text-xs text-fg-dim truncate">
                    {r.of_username ? `@${r.of_username} · ` : ""}id {r.fan_id}
                    {r.added_at ? ` · ${new Date(r.added_at).toLocaleDateString()}` : ""}
                  </div>
                </div>
                <Button
                  variant="ghost"
                  disabled={unrestrictOne.isPending}
                  onClick={() => unrestrictOne.mutate(r.fan_id)}
                  title="Unrestrict — resume automations for this fan"
                >
                  <Trash2 size={15} />
                </Button>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}
