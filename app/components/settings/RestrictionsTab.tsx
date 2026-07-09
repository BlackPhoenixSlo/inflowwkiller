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
 * them. The first card is the MANUAL automations list only.
 *
 * Second card: users restricted ON ONLYFANS from the chat ⋯ menu ("Restrict on
 * OnlyFans", always available per model). Restricting sends one "." + marks the
 * chat read first so the thread isn't left flagged unread, then writes the
 * durable of_restricted registry: excluded from every unread/owe-reply count
 * (roster badges + inbox chips) and hard-skipped by every automation. Lifting
 * here un-restricts on OF too.
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

  // Users restricted ON ONLYFANS via our UI (durable of_restricted registry) —
  // per model, same account selector as the card above.
  const ofListQ = useQuery({
    queryKey: ["of-restricted-list", accountId],
    queryFn: () =>
      relay.get<{ count: number; list: RestrictRow[] }>(
        "/api/of/v2/of-restricted", { accountId: accountId! }),
    enabled: !!accountId,
  });
  const liftOfRestrict = useMutation({
    mutationFn: (fanId: number) =>
      relay.delete(`/api/of/v2/users/${fanId}/restrict`, { accountId: accountId! }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["of-restricted-list", accountId] });
      qc.invalidateQueries({ queryKey: ["automation-restrict"] });
      // Back in the unread counts + conversation set from the next poll.
      qc.invalidateQueries({ queryKey: ["chats"] });
      qc.invalidateQueries({ queryKey: ["roster-counts"] });
    },
  });

  const rows = listQ.data?.list ?? [];
  const ofRows = ofListQ.data?.list ?? [];

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

      <Card className="p-4 space-y-3">
        <div>
          <h3 className="text-sm font-semibold flex items-center gap-2">
            <Ban size={15} /> Restricted on OnlyFans
          </h3>
          <p className="text-xs text-fg-dim mt-1 max-w-2xl">
            Restricted from the chat ⋯ menu (&ldquo;Restrict on OnlyFans&rdquo;):
            OF stops delivering their messages, they&apos;re excluded from every
            unread count, and no automation ever messages them. Restricting sends
            one &ldquo;.&rdquo; and marks the chat read first so the thread
            isn&apos;t left flagged unread. Lifting here un-restricts on OnlyFans
            too.
          </p>
        </div>

        <span className="text-sm text-fg-dim">
          {ofListQ.isLoading
            ? "Loading…"
            : `${ofRows.length} user${ofRows.length === 1 ? "" : "s"} restricted on OF`}
        </span>

        {!ofListQ.isLoading && ofRows.length === 0 && (
          <div className="text-sm text-fg-dim py-6 text-center">
            No users restricted on OnlyFans from this app.
          </div>
        )}

        {ofRows.length > 0 && (
          <ul className="divide-y divide-border">
            {ofRows.map((r) => (
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
                  disabled={liftOfRestrict.isPending}
                  onClick={() => liftOfRestrict.mutate(r.fan_id)}
                  title="Lift the OnlyFans restrict — their messages and unread counts resume"
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
