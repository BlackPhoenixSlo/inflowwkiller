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
import { describeDeadSession, fmtRelTime } from "@/lib/utils";

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
    timed_out?: boolean;
    message?: string;
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
    // Still a live OF round-trip per account (bounded + budgeted server-side,
    // see server.py's `_health_all_accounts`), so keep the table responsive by
    // showing accountsQ data first and overlaying health when it lands.
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
    onSuccess: (_data, id) => {
      qc.invalidateQueries({ queryKey: ["accounts"] });
      // Hard-EVICT every per-account cache for the dropped model so a manual
      // logout/login is no longer needed to clear it. Per-account query keys
      // embed the accountId somewhere in the key — ["chats", kind, accountId,
      // ...], ["vault-media", accountId], ["of-user", accountId, fanId],
      // ["fan", accountId, ...], *-config / *-rules / automation-* / nudge-*,
      // etc. A predicate scan over the live cache removes them all without a
      // hand-maintained prefix list. removeQueries (not invalidate) drops them
      // from memory, which also evicts the persisted localStorage snapshot
      // since the persister only dehydrates live queries. The split(",") check
      // also matches the unified ("all" scope) chat key, whose accountKey is a
      // comma-joined list of every fanned-out account id (useChatList.ts).
      qc.removeQueries({
        predicate: (q) =>
          q.queryKey.some(
            (part) =>
              typeof part === "string" &&
              (part === id || part.split(",").includes(id)),
          ),
      });
    },
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
    const orig = account.nickname || account.id;
    if (draft.trim() && draft.trim() !== orig) {
      onRename(draft.trim());
    }
  };

  let statusBadge = <Badge color="muted">no session</Badge>;
  if (account.has_session) {
    if (!health) {
      statusBadge = <Badge color="muted">probing…</Badge>;
    } else if (health.ok) {
      statusBadge = <Badge color="ok">live · {health.name || "OF"}</Badge>;
    } else if (health.timed_out) {
      // A probe that outran the relay's budget is NOT a verdict on the session:
      // the relay stopped waiting, it did not cancel, and the OF call is still
      // running upstream. Red here would flag a healthy-but-slow account as
      // dead, which is the one failure the budget must not manufacture.
      statusBadge = <Badge color="warn" title={health.message}>still probing…</Badge>;
    } else {
      statusBadge = <Badge color="err">{health.error || "down"}</Badge>;
    }
  }
  // A flagged session OUTRANKS the live probe: it pauses every automation for
  // the account (service/account_health.py), and neither of the branches above
  // can say so — a parked account renders as a plain "no session" or "down",
  // side by side with accounts that are genuinely working. That silence is the
  // whole bug: rules keep showing as enabled while nothing runs.
  if (account.session_dead_at) {
    statusBadge = (
      <span className="flex items-center gap-1.5 flex-wrap">
        <Badge color="err" title={describeDeadSession(account.session_dead_reason)}>
          automations paused
        </Badge>
        <span className="text-[11px] text-fg-dim whitespace-nowrap">
          unlinked {fmtRelTime(account.session_dead_at)}
        </span>
      </span>
    );
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
