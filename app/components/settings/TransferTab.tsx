"use client";

/**
 * TransferTab — hand one of your models OR employees over to another
 * friend. You can only transfer things you currently own (ownership is
 * the auth — no password). Two independent cards: Model + Employee.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";

import { Button, Card } from "@/components/ui/primitives";
import { relay, type Employee } from "@/lib/relay";
import { useUser } from "@/contexts/UserContext";

interface AdminUserRow {
  id: string;
  username: string;
  created_at: string;
  last_seen_at: string;
  account_count: number;
}

interface GrantedAccount {
  account_id: string;
  nickname: string | null;
  color: string | null;
}

interface GrantOverview {
  granted: GrantedAccount[];
  available: GrantedAccount[];
}

interface ModelTransferResp {
  ok: boolean;
  from_user_id: string;
  to_user_id: string;
  account_id: string;
}

interface EmployeeTransferResp {
  ok: boolean;
  id: number;
  display_name: string;
  user_id: string;
}

interface EmployeesResp { employees: Employee[]; }

export default function TransferTab() {
  const { user: me } = useUser();
  const usersQ = useQuery<AdminUserRow[]>({
    queryKey: ["admin", "users"],
    queryFn: () => relay.get<AdminUserRow[]>("/admin/users"),
    staleTime: 30_000,
  });

  const users: AdminUserRow[] = Array.isArray(usersQ.data) ? usersQ.data : [];
  const destOptions = useMemo(
    () => users.filter((u) => u.id !== me?.user_id),
    [users, me?.user_id],
  );

  return (
    <div className="space-y-4">
      <ModelTransferCard
        me={me}
        usersQ={usersQ}
        users={users}
        destOptions={destOptions}
      />
      <EmployeeTransferCard
        me={me}
        usersQ={usersQ}
        users={users}
        destOptions={destOptions}
      />
    </div>
  );
}

// ── Model transfer ─────────────────────────────────────────────────

interface CardProps {
  me: { user_id: string; username: string } | null;
  usersQ: ReturnType<typeof useQuery<AdminUserRow[]>>;
  users: AdminUserRow[];
  destOptions: AdminUserRow[];
}

function ModelTransferCard({ me, usersQ, users, destOptions }: CardProps) {
  const qc = useQueryClient();
  const [toUserId, setToUserId] = useState("");
  const [accountId, setAccountId] = useState("");
  const [ok, setOk] = useState<string | null>(null);

  const myAccountsQ = useQuery<GrantOverview>({
    queryKey: ["admin", "users", me?.user_id, "accounts"],
    queryFn: () => relay.get<GrantOverview>(
      `/admin/users/${encodeURIComponent(me!.user_id)}/accounts`,
    ),
    enabled: !!me?.user_id,
    staleTime: 30_000,
  });

  const myAccounts: GrantedAccount[] = Array.isArray(myAccountsQ.data?.granted)
    ? myAccountsQ.data!.granted
    : [];

  const transferM = useMutation({
    mutationFn: () =>
      relay.post<ModelTransferResp>("/admin/users/transfer", {
        to_user_id: toUserId,
        account_id: accountId,
      }),
    onSuccess: (resp) => {
      const toUsername = users.find((u) => u.id === resp.to_user_id)?.username;
      const label = myAccounts.find(
        (a) => a.account_id === resp.account_id,
      )?.nickname ?? resp.account_id;
      setOk(`Transferred ${label} → @${toUsername ?? resp.to_user_id}`);
      setAccountId("");
      setToUserId("");
      qc.invalidateQueries({ queryKey: ["admin", "users"] });
      qc.invalidateQueries({ queryKey: ["admin", "users", me?.user_id, "accounts"] });
      qc.invalidateQueries({ queryKey: ["accounts"] });
    },
  });

  const canSubmit =
    !!toUserId &&
    toUserId !== me?.user_id &&
    !!accountId &&
    !transferM.isPending;

  function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!canSubmit) return;
    setOk(null);
    transferM.mutate();
  }

  return (
    <Card>
      <div className="space-y-1 mb-4">
        <h2 className="text-base font-semibold">Transfer model</h2>
        <p className="text-xs text-fg-dim">
          Hand one of your own models to another friend. You lose
          access, they gain it.
        </p>
      </div>

      {usersQ.isLoading || myAccountsQ.isLoading ? (
        <div className="text-sm text-fg-dim">Loading…</div>
      ) : usersQ.error ? (
        <div className="text-sm text-err">
          {(usersQ.error as Error).message || "failed to load users"}
        </div>
      ) : destOptions.length === 0 ? (
        <div className="text-sm text-fg-dim">
          No other users to transfer to.
        </div>
      ) : myAccounts.length === 0 ? (
        <div className="text-sm text-fg-dim">
          You don&apos;t own any models to transfer.
        </div>
      ) : (
        <form onSubmit={onSubmit} className="grid gap-4 max-w-xl">
          <label className="grid gap-1 text-sm">
            <span className="text-fg-dim">Model</span>
            <select
              value={accountId}
              onChange={(e) => setAccountId(e.target.value)}
              className="px-3 py-2 rounded-lg bg-bg border border-border focus:outline-none focus:border-fg/30"
            >
              <option value="">— pick one of your models —</option>
              {myAccounts.map((a) => (
                <option key={a.account_id} value={a.account_id}>
                  {a.nickname || a.account_id} ({a.account_id})
                </option>
              ))}
            </select>
          </label>

          <label className="grid gap-1 text-sm">
            <span className="text-fg-dim">To</span>
            <select
              value={toUserId}
              onChange={(e) => setToUserId(e.target.value)}
              className="px-3 py-2 rounded-lg bg-bg border border-border focus:outline-none focus:border-fg/30"
            >
              <option value="">— pick a user —</option>
              {destOptions.map((u) => (
                <option key={u.id} value={u.id}>
                  @{u.username}
                </option>
              ))}
            </select>
          </label>

          {transferM.error && (
            <div className="text-sm text-err" role="alert">
              {(transferM.error as Error).message || "transfer failed"}
            </div>
          )}
          {ok && (
            <div className="text-sm text-emerald-400" role="status">
              {ok}
            </div>
          )}

          <div>
            <Button type="submit" disabled={!canSubmit} variant="primary">
              {transferM.isPending ? "Transferring…" : "Transfer model"}
            </Button>
          </div>
        </form>
      )}
    </Card>
  );
}

// ── Employee transfer ──────────────────────────────────────────────

function EmployeeTransferCard({ me, usersQ, users, destOptions }: CardProps) {
  const qc = useQueryClient();
  const [toUserId, setToUserId] = useState("");
  const [employeeId, setEmployeeId] = useState("");
  const [ok, setOk] = useState<string | null>(null);

  // Mirrors EmployeesTab's query — same key so they share cache. The
  // signed-in user only ever sees their own roster (employees.py filters
  // by user_id), so we can render the response as-is.
  const myEmployeesQ = useQuery<EmployeesResp>({
    queryKey: ["employees", "all"],
    queryFn: () =>
      relay.get<EmployeesResp>("/admin/employees?include_disabled=true"),
    staleTime: 10_000,
  });

  // Chatter-mirror Employee rows (chatter_id != null) are auto-created
  // for chatter logins under THIS owner. Transferring one to another
  // friend would orphan the mirror — the destination user isn't in the
  // chatter's `chatter_users` set, so the chatter's next sign-in
  // wouldn't resolve to it. Hide them from the picker; the backend
  // also rejects with 400 as defense-in-depth.
  const myEmployees: Employee[] = (Array.isArray(myEmployeesQ.data?.employees)
    ? myEmployeesQ.data!.employees
    : []
  ).filter((e) => !e.chatter_id);

  const transferM = useMutation({
    mutationFn: () =>
      relay.post<EmployeeTransferResp>(
        `/admin/employees/${encodeURIComponent(employeeId)}/transfer`,
        { to_user_id: toUserId },
      ),
    onSuccess: (resp) => {
      const toUsername = users.find((u) => u.id === resp.user_id)?.username;
      setOk(`Transferred ${resp.display_name} → @${toUsername ?? resp.user_id}`);
      setEmployeeId("");
      setToUserId("");
      qc.invalidateQueries({ queryKey: ["employees"] });
    },
  });

  const canSubmit =
    !!toUserId &&
    toUserId !== me?.user_id &&
    !!employeeId &&
    !transferM.isPending;

  function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!canSubmit) return;
    setOk(null);
    transferM.mutate();
  }

  return (
    <Card>
      <div className="space-y-1 mb-4">
        <h2 className="text-base font-semibold">Transfer employee</h2>
        <p className="text-xs text-fg-dim">
          Hand one of your chatters to another friend. Their audit
          history stays attached; per-account access grants are wiped
          since they wouldn&apos;t map to the new owner&apos;s models.
        </p>
      </div>

      {usersQ.isLoading || myEmployeesQ.isLoading ? (
        <div className="text-sm text-fg-dim">Loading…</div>
      ) : usersQ.error ? (
        <div className="text-sm text-err">
          {(usersQ.error as Error).message || "failed to load users"}
        </div>
      ) : destOptions.length === 0 ? (
        <div className="text-sm text-fg-dim">
          No other users to transfer to.
        </div>
      ) : myEmployees.length === 0 ? (
        <div className="text-sm text-fg-dim">
          You don&apos;t have any employees to transfer.
        </div>
      ) : (
        <form onSubmit={onSubmit} className="grid gap-4 max-w-xl">
          <label className="grid gap-1 text-sm">
            <span className="text-fg-dim">Employee</span>
            <select
              value={employeeId}
              onChange={(e) => setEmployeeId(e.target.value)}
              className="px-3 py-2 rounded-lg bg-bg border border-border focus:outline-none focus:border-fg/30"
            >
              <option value="">— pick one of your chatters —</option>
              {myEmployees.map((emp) => (
                <option key={emp.id} value={String(emp.id)}>
                  {emp.display_name}
                  {emp.is_active ? "" : " (disabled)"}
                </option>
              ))}
            </select>
          </label>

          <label className="grid gap-1 text-sm">
            <span className="text-fg-dim">To</span>
            <select
              value={toUserId}
              onChange={(e) => setToUserId(e.target.value)}
              className="px-3 py-2 rounded-lg bg-bg border border-border focus:outline-none focus:border-fg/30"
            >
              <option value="">— pick a user —</option>
              {destOptions.map((u) => (
                <option key={u.id} value={u.id}>
                  @{u.username}
                </option>
              ))}
            </select>
          </label>

          {transferM.error && (
            <div className="text-sm text-err" role="alert">
              {(transferM.error as Error).message || "transfer failed"}
            </div>
          )}
          {ok && (
            <div className="text-sm text-emerald-400" role="status">
              {ok}
            </div>
          )}

          <div>
            <Button type="submit" disabled={!canSubmit} variant="primary">
              {transferM.isPending ? "Transferring…" : "Transfer employee"}
            </Button>
          </div>
        </form>
      )}
    </Card>
  );
}
