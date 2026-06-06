"use client";

/**
 * AccountsTable — every OF model account the relay knows about, with
 * its health, color, and quick actions (rename, mark default, delete).
 *
 * Powered by:
 *   GET  /admin/accounts            list + active
 *   GET  /health?all_accounts=1     per-account live OF probe
 *   PATCH /admin/accounts/{id}      rename / recolor
 *   POST /admin/accounts/active     mark as default
 *   DELETE /admin/accounts/{id}     remove (soft on disk; sessions kept)
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { relay, type AccountMeta } from "@/lib/relay";
import { Badge, Button, Card, Input } from "@/components/ui/primitives";

interface AccountsResp {
  accounts: AccountMeta[];
  active_account_id: string | null;
}
interface HealthAllResp {
  ok: boolean;
  accounts: Array<{
    account_id: string;
    nickname?: string | null;
    ok: boolean;
    name?: string;
    error?: string;
    upstream_body?: string;
    upstream_status?: number;
    proxy?: { label?: string | null; url?: string | null } | null;
  }>;
}

export default function AccountsTable() {
  const qc = useQueryClient();

  const accountsQ = useQuery<AccountsResp>({
    queryKey: ["accounts"],
    queryFn: () => relay.get<AccountsResp>("/admin/accounts"),
    staleTime: 30_000,
  });

  const healthQ = useQuery<HealthAllResp>({
    queryKey: ["health-all"],
    queryFn: () => relay.get<HealthAllResp>("/health?all_accounts=1"),
    staleTime: 60_000,
    // Probing live OF can take 2-5s per account; keep the table
    // responsive by showing accountsQ data first and overlaying health.
  });

  const renameM = useMutation({
    mutationFn: ({ id, nickname }: { id: string; nickname: string }) =>
      relay.patch<unknown>(`/admin/accounts/${encodeURIComponent(id)}`, { nickname }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["accounts"] }),
  });

  const activateM = useMutation({
    mutationFn: (account_id: string) =>
      relay.post<unknown>("/admin/accounts/active", { account_id }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["accounts"] }),
  });

  const deleteM = useMutation({
    mutationFn: (id: string) =>
      relay.delete<unknown>(`/admin/accounts/${encodeURIComponent(id)}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["accounts"] }),
  });

  const healthByAid = new Map(
    (healthQ.data?.accounts || []).map((h) => [h.account_id, h]),
  );

  return (
    <Card>
      <div className="flex items-center justify-between mb-4">
        <h2 className="text-base font-semibold">Accounts</h2>
        <Badge color="muted">{accountsQ.data?.accounts.length ?? "…"} model{(accountsQ.data?.accounts.length ?? 0) === 1 ? "" : "s"}</Badge>
      </div>

      {accountsQ.isLoading && (
        <div className="text-fg-dim text-sm py-6">Loading accounts…</div>
      )}
      {accountsQ.error && (
        <div className="text-err text-sm py-3">{(accountsQ.error as Error).message}</div>
      )}

      {accountsQ.data && (
        <div className="overflow-x-auto -mx-1">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-fg-dim text-xs border-b border-border">
                <th className="px-3 py-2 font-medium">Account</th>
                <th className="px-3 py-2 font-medium">user_id</th>
                <th className="px-3 py-2 font-medium">Status</th>
                <th className="px-3 py-2 font-medium">Last used</th>
                <th className="px-3 py-2 font-medium">Actions</th>
              </tr>
            </thead>
            <tbody>
              {accountsQ.data.accounts.map((a) => (
                <Row
                  key={a.id}
                  account={a}
                  health={healthByAid.get(a.id)}
                  isDefault={accountsQ.data.active_account_id === a.id}
                  onRename={(nickname) => renameM.mutate({ id: a.id, nickname })}
                  onActivate={() => activateM.mutate(a.id)}
                  onDelete={() => {
                    if (confirm(`Delete account ${a.nickname || a.id}? Sessions stay on disk.`)) {
                      deleteM.mutate(a.id);
                    }
                  }}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}

function Row({
  account,
  health,
  isDefault,
  onRename,
  onActivate,
  onDelete,
}: {
  account: AccountMeta;
  health?: HealthAllResp["accounts"][number];
  isDefault: boolean;
  onRename: (n: string) => void;
  onActivate: () => void;
  onDelete: () => void;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(account.nickname || account.id);

  const commitRename = () => {
    setEditing(false);
    if (draft.trim() && draft.trim() !== account.nickname) {
      onRename(draft.trim());
    }
  };

  let statusBadge = <Badge color="muted">no session</Badge>;
  if (account.has_session) {
    if (!health) {
      statusBadge = <Badge color="muted">probing…</Badge>;
    } else if (health.ok) {
      statusBadge = <Badge color="ok">live · {health.name || "OF"}</Badge>;
    } else {
      statusBadge = <Badge color="err">{health.error || "down"}</Badge>;
    }
  }

  return (
    <tr className="border-b border-border last:border-0">
      <td className="px-3 py-3">
        <div className="flex items-center gap-2.5">
          <span
            className="w-2.5 h-2.5 rounded-full shrink-0"
            style={{ background: account.color || "#888" }}
            aria-hidden
          />
          {editing ? (
            <Input
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onBlur={commitRename}
              onKeyDown={(e) => {
                if (e.key === "Enter") commitRename();
                if (e.key === "Escape") { setEditing(false); setDraft(account.nickname || account.id); }
              }}
              autoFocus
              className="w-44 py-1 text-sm"
            />
          ) : (
            <button
              type="button"
              onClick={() => setEditing(true)}
              className="hover:text-accent text-left"
              title="Click to rename"
            >
              {account.nickname || account.id}
            </button>
          )}
        </div>
      </td>
      <td className="px-3 py-3 font-mono text-xs text-fg-dim">{account.id}</td>
      <td className="px-3 py-3">
        <div className="flex items-center gap-2">
          {statusBadge}
          {isDefault && <Badge color="muted">default</Badge>}
        </div>
      </td>
      <td className="px-3 py-3 text-xs text-fg-dim">
        {account.last_used_at?.slice(0, 16).replace("T", " ") ?? "—"}
      </td>
      <td className="px-3 py-3">
        <div className="flex items-center gap-2">
          {!isDefault && (
            <Button variant="ghost" size="sm" onClick={onActivate}>
              Make default
            </Button>
          )}
          <Button variant="danger" size="sm" onClick={onDelete}>
            Delete
          </Button>
        </div>
      </td>
    </tr>
  );
}
