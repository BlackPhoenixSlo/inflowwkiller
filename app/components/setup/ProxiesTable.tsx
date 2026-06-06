"use client";

/**
 * ProxiesTable — every proxy in the registry, with create + assign + test + remove.
 *
 * Powered by:
 *   GET    /admin/proxies                 list + assignments enriched with account meta
 *   POST   /admin/proxies                 upsert {label, host, port, scheme, username?, password?, notes?}
 *   DELETE /admin/proxies/{label}         remove
 *   POST   /admin/proxies/assign          bind a proxy to an account_id (or null to unassign)
 *   POST   /admin/proxies/{label}/test    egress-IP check
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { relay, type ProxyMeta, type AccountMeta } from "@/lib/relay";
import { Badge, Button, Card, Input } from "@/components/ui/primitives";

interface ProxiesResp {
  proxies: Array<
    ProxyMeta & {
      // Backend now returns a list (many accounts may share a proxy);
      // `assigned_account` (singular) is kept around as legacy mirror.
      assigned_accounts?: Array<{ id: string; nickname?: string | null; color?: string | null }>;
      assigned_account?: { id: string; nickname?: string | null } | null;
    }
  >;
  accounts: AccountMeta[];
}

interface ProxyDraft {
  label: string;
  scheme: "http" | "https" | "socks5";
  host: string;
  port: string;
  username: string;
  password: string;
  notes: string;
}

const EMPTY_DRAFT: ProxyDraft = {
  label: "", scheme: "http", host: "", port: "", username: "", password: "", notes: "",
};

export default function ProxiesTable() {
  const qc = useQueryClient();
  const [addOpen, setAddOpen] = useState(false);
  const [draft, setDraft] = useState<ProxyDraft>(EMPTY_DRAFT);
  const [draftError, setDraftError] = useState<string | null>(null);

  const proxiesQ = useQuery<ProxiesResp>({
    queryKey: ["proxies"],
    queryFn: () => relay.get<ProxiesResp>("/admin/proxies"),
    staleTime: 60_000,
  });

  // Single mutation key, used for both bind and unbind (same per-row spinner
  // semantics — `variables.label` lets a row know which action is in flight).
  const assignM = useMutation({
    mutationFn: ({ label, account_id }: { label: string; account_id: string }) =>
      relay.post<unknown>("/admin/proxies/assign", { label, account_id }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["proxies"] });
      qc.invalidateQueries({ queryKey: ["accounts"] });
    },
  });

  const unbindM = useMutation({
    mutationFn: ({ label, account_id }: { label: string; account_id: string }) =>
      relay.post<unknown>(`/admin/proxies/${encodeURIComponent(label)}/unbind`, { account_id }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["proxies"] });
      qc.invalidateQueries({ queryKey: ["accounts"] });
    },
  });

  const testM = useMutation({
    mutationFn: (label: string) =>
      relay.post<{ ok: boolean; egress_ip?: string; error?: string }>(
        `/admin/proxies/${encodeURIComponent(label)}/test`,
      ),
  });

  const createM = useMutation({
    mutationFn: (body: Record<string, unknown>) =>
      relay.post<{ ok: boolean; proxy: ProxyMeta }>("/admin/proxies", body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["proxies"] });
      setDraft(EMPTY_DRAFT);
      setDraftError(null);
      setAddOpen(false);
    },
    onError: (err: Error) => setDraftError(err.message),
  });

  // The relay's UPSERT semantics means hitting POST with an existing label
  // overwrites in place — we treat that as "edit" but for now the inline
  // form only opens for net-new entries. Duplicate-label guard runs here
  // so a typo doesn't silently clobber a working proxy.
  const removeM = useMutation({
    mutationFn: (label: string) => relay.delete<unknown>(`/admin/proxies/${encodeURIComponent(label)}`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["proxies"] });
      qc.invalidateQueries({ queryKey: ["accounts"] });
    },
  });

  function submitDraft() {
    setDraftError(null);
    const label = draft.label.trim();
    const host = draft.host.trim();
    const portN = Number(draft.port);
    if (!label) return setDraftError("Label is required");
    if (!host)  return setDraftError("Host is required");
    if (!Number.isInteger(portN) || portN <= 0 || portN > 65535) {
      return setDraftError("Port must be 1–65535");
    }
    if (proxiesQ.data?.proxies.some((p) => p.label === label)) {
      return setDraftError(`A proxy named "${label}" already exists`);
    }
    createM.mutate({
      label,
      scheme: draft.scheme,
      host,
      port: portN,
      username: draft.username.trim() || null,
      password: draft.password || null,
      notes: draft.notes.trim(),
    });
  }

  return (
    <Card>
      <div className="flex items-center justify-between mb-4 gap-2">
        <div className="flex items-center gap-2">
          <h2 className="text-base font-semibold">Proxies</h2>
          <Badge color="muted">{proxiesQ.data?.proxies.length ?? "…"} configured</Badge>
        </div>
        <Button
          size="sm"
          variant={addOpen ? "ghost" : "secondary"}
          onClick={() => {
            setAddOpen((v) => !v);
            setDraftError(null);
          }}
        >
          {addOpen ? "Cancel" : "+ Add proxy"}
        </Button>
      </div>

      {addOpen && (
        <div className="mb-4 border border-border rounded-lg p-3 bg-bg-elev-1/40 space-y-2">
          <div className="grid grid-cols-1 sm:grid-cols-[1fr_120px_2fr_100px] gap-2">
            <Input
              placeholder="Label (e.g. hu-4)"
              value={draft.label}
              onChange={(e) => setDraft({ ...draft, label: e.target.value })}
              autoFocus
            />
            <select
              value={draft.scheme}
              onChange={(e) => setDraft({ ...draft, scheme: e.target.value as ProxyDraft["scheme"] })}
              className="bg-bg border border-border rounded-lg px-2 py-2 text-sm focus:outline-none focus:border-accent"
            >
              <option value="http">http</option>
              <option value="https">https</option>
              <option value="socks5">socks5</option>
            </select>
            <Input
              placeholder="Host (188.244.113.72)"
              value={draft.host}
              onChange={(e) => setDraft({ ...draft, host: e.target.value })}
            />
            <Input
              placeholder="Port"
              inputMode="numeric"
              value={draft.port}
              onChange={(e) => setDraft({ ...draft, port: e.target.value.replace(/[^0-9]/g, "") })}
            />
          </div>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
            <Input
              placeholder="Username (optional)"
              value={draft.username}
              onChange={(e) => setDraft({ ...draft, username: e.target.value })}
              autoComplete="off"
            />
            <Input
              type="password"
              placeholder="Password (optional)"
              value={draft.password}
              onChange={(e) => setDraft({ ...draft, password: e.target.value })}
              autoComplete="new-password"
            />
          </div>
          <Input
            placeholder="Notes (e.g. Budapest, HU AS211439)"
            value={draft.notes}
            onChange={(e) => setDraft({ ...draft, notes: e.target.value })}
          />
          <div className="flex items-center justify-between gap-2 pt-1">
            <div className="text-[11px] text-fg-dim">
              Credentials are stored in <code>service/proxies.json</code> in plain text (phase-1).
            </div>
            <div className="flex items-center gap-2">
              {draftError && <span className="text-err text-[11px]">{draftError}</span>}
              <Button
                size="sm"
                onClick={submitDraft}
                disabled={createM.isPending}
              >
                {createM.isPending ? "Saving…" : "Save proxy"}
              </Button>
            </div>
          </div>
        </div>
      )}

      {proxiesQ.isLoading && (
        <div className="text-fg-dim text-sm py-6">Loading proxies…</div>
      )}
      {proxiesQ.error && (
        <div className="text-err text-sm py-3">{(proxiesQ.error as Error).message}</div>
      )}

      {proxiesQ.data && (
        <div className="overflow-x-auto -mx-1">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-fg-dim text-xs border-b border-border">
                <th className="px-3 py-2 font-medium">Label</th>
                <th className="px-3 py-2 font-medium">Host</th>
                <th className="px-3 py-2 font-medium">Bound to</th>
                <th className="px-3 py-2 font-medium">Egress / verified</th>
                <th className="px-3 py-2 font-medium">Actions</th>
              </tr>
            </thead>
            <tbody>
              {proxiesQ.data.proxies.map((p) => (
                <ProxyRow
                  key={p.label}
                  proxy={p}
                  accounts={proxiesQ.data!.accounts}
                  onBind={(account_id) => assignM.mutate({ label: p.label, account_id })}
                  onUnbind={(account_id) => unbindM.mutate({ label: p.label, account_id })}
                  onTest={() => testM.mutate(p.label)}
                  onRemove={() => {
                    const n = p.assigned_accounts?.length ?? 0;
                    const ext = n > 0
                      ? ` ${n} account${n === 1 ? "" : "s"} will fall back to direct egress until reassigned.`
                      : "";
                    if (confirm(`Remove proxy "${p.label}"?${ext}`)) {
                      removeM.mutate(p.label);
                    }
                  }}
                  testResult={testM.variables === p.label ? testM.data : undefined}
                  testing={testM.isPending && testM.variables === p.label}
                  removing={removeM.isPending && removeM.variables === p.label}
                  busyBind={assignM.isPending && assignM.variables?.label === p.label}
                  busyUnbind={unbindM.isPending && unbindM.variables?.label === p.label}
                />
              ))}
              {proxiesQ.data.proxies.length === 0 && (
                <tr>
                  <td colSpan={5} className="px-3 py-6 text-center text-fg-dim text-xs">
                    No proxies configured yet — click <span className="text-fg">+ Add proxy</span> to register one.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}

function ProxyRow({
  proxy, accounts, onBind, onUnbind, onTest, onRemove,
  testResult, testing, removing, busyBind, busyUnbind,
}: {
  proxy: ProxiesResp["proxies"][number];
  accounts: AccountMeta[];
  onBind: (account_id: string) => void;
  onUnbind: (account_id: string) => void;
  onTest: () => void;
  onRemove: () => void;
  testResult?: { ok: boolean; egress_ip?: string; error?: string };
  testing: boolean;
  removing: boolean;
  busyBind: boolean;
  busyUnbind: boolean;
}) {
  // Server returns `assigned_accounts: [...]`; fall back to the legacy
  // singular so a stale relay (running on an old build) doesn't blank the
  // cell. Sort by nickname for stable rendering across refetches.
  const bound = (proxy.assigned_accounts && proxy.assigned_accounts.length > 0)
    ? proxy.assigned_accounts
    : proxy.assigned_account ? [proxy.assigned_account] : [];
  const boundIds = new Set(bound.map((b) => b.id));
  const unbound = accounts.filter((a) => !boundIds.has(a.id));
  const shared = bound.length > 1;
  return (
    <tr className="border-b border-border last:border-0">
      <td className="px-3 py-3">
        <div className="flex items-center gap-2">
          <span className="font-medium">{proxy.label}</span>
          <Badge color="muted">{proxy.scheme}</Badge>
        </div>
      </td>
      <td className="px-3 py-3 font-mono text-xs text-fg-dim">
        {proxy.host}:{proxy.port}
      </td>
      <td className="px-3 py-3 align-top">
        <div className="flex flex-wrap items-center gap-1.5">
          {bound.length === 0 && (
            <span className="text-fg-dim text-[11px] italic">unassigned</span>
          )}
          {bound.map((b) => {
            const meta = accounts.find((a) => a.id === b.id);
            const color = meta?.color || (b as { color?: string | null }).color || "#666";
            return (
              <span
                key={b.id}
                className="inline-flex items-center gap-1.5 pl-1.5 pr-1 py-0.5 rounded-full text-[11px] border border-border bg-bg-elev-1"
              >
                <span className="w-2 h-2 rounded-full" style={{ background: color }} />
                <span className="truncate max-w-[140px]">{b.nickname || b.id}</span>
                <button
                  type="button"
                  onClick={() => onUnbind(b.id)}
                  disabled={busyUnbind}
                  title="Unbind from this proxy"
                  className="text-fg-dim hover:text-err leading-none px-1 disabled:opacity-50"
                  aria-label={`Unbind ${b.nickname || b.id}`}
                >
                  ×
                </button>
              </span>
            );
          })}
          {shared && (
            <span
              title="More than one OF account shares this proxy. OF can correlate accounts that egress from the same IP — only do this for accounts you intend to link."
              className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-full text-[11px] font-medium border bg-warn/15 text-warn border-warn/30 cursor-help"
            >
              <span aria-hidden>⚠</span>
              <span>shared egress</span>
            </span>
          )}
          {unbound.length > 0 && (
            <select
              value=""
              onChange={(e) => {
                if (e.target.value) onBind(e.target.value);
              }}
              disabled={busyBind}
              className="bg-bg border border-border rounded-md px-2 py-1 text-[11px] disabled:opacity-50"
            >
              <option value="">{bound.length === 0 ? "+ assign account" : "+ add model"}</option>
              {unbound.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.nickname || a.id}
                </option>
              ))}
            </select>
          )}
        </div>
      </td>
      <td className="px-3 py-3 text-xs">
        {testResult ? (
          testResult.ok ? (
            <Badge color="ok">{testResult.egress_ip}</Badge>
          ) : (
            <Badge color="err">{testResult.error || "failed"}</Badge>
          )
        ) : (
          <span className="font-mono text-fg-dim">
            {proxy.verified_ip || "—"}
          </span>
        )}
      </td>
      <td className="px-3 py-3">
        <div className="flex items-center gap-1">
          <Button variant="ghost" size="sm" disabled={testing} onClick={onTest}>
            {testing ? "Testing…" : "Test"}
          </Button>
          <Button variant="danger" size="sm" disabled={removing} onClick={onRemove}>
            {removing ? "Removing…" : "Remove"}
          </Button>
        </div>
      </td>
    </tr>
  );
}
