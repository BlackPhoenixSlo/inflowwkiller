"use client";

/**
 * ChattersTab — owner-side management of linked chatters.
 *
 * Three sections:
 *   • Your chatters — rows linked to the current owner. Per-row Unlink
 *     button (optimistic; rollback on error).
 *   • Invite chatter — mint a one-shot invite URL the owner shares.
 *   • Link existing chatter — type a username; 404 inline-error guides
 *     the owner to share an invite link instead.
 *
 * All mutations follow the onMutate/setQueryData/onError-rollback/
 * onSettled-invalidate pattern from EmployeesTab so the UI never blocks
 * on the network round-trip.
 *
 * Server endpoints (see service/chatters.py admin_router):
 *   GET    /admin/chatters
 *   POST   /admin/chatters/link
 *   DELETE /admin/chatters/{id}/link
 *   POST   /admin/chatters/invite
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useState } from "react";

import { Button, Card } from "@/components/ui/primitives";
import { relay, RelayError, type VaultListsResp } from "@/lib/relay";
import { fetchAllVaultLists } from "@/hooks/useVaultMedia";
import { useAccounts } from "@/hooks/useAccounts";
import { type FanId } from "@/lib/fanId";

interface ChatterRow {
  id: string;
  username: string;
  display_name: string | null;
  color: string | null;
  is_active: boolean;
  employee_id_for_this_owner: number | null;
  created_at: string;
  last_seen_at: string;
}

interface InviteResp {
  token: string;
  expires_at: string;
  accept_path: string;
}

const QK = ["admin", "chatters"] as const;

interface AccessResp {
  account_ids: string[];
  folder_access: Record<string, number[]>;
}

export default function ChattersTab() {
  const qc = useQueryClient();
  // Which chatter row has its Access editor expanded (at most one).
  const [accessFor, setAccessFor] = useState<string | null>(null);

  const q = useQuery<ChatterRow[]>({
    queryKey: QK,
    queryFn: () => relay.get<ChatterRow[]>("/admin/chatters"),
    staleTime: 10_000,
  });

  const unlinkM = useMutation({
    mutationFn: (id: string) => relay.delete(`/admin/chatters/${id}/link`),
    onMutate: async (id: string) => {
      await qc.cancelQueries({ queryKey: QK });
      const prev = qc.getQueryData<ChatterRow[]>(QK);
      if (prev) {
        qc.setQueryData<ChatterRow[]>(QK, prev.filter((r) => r.id !== id));
      }
      return { prev };
    },
    onError: (_err, _id, ctx) => {
      if (ctx?.prev) qc.setQueryData(QK, ctx.prev);
    },
    onSettled: () => qc.invalidateQueries({ queryKey: QK }),
  });

  const linkM = useMutation({
    mutationFn: (body: { username: string; display_name?: string }) =>
      relay.post<{ ok: boolean; chatter_id: string; username: string; already_linked: boolean }>(
        "/admin/chatters/link", body,
      ),
    // No optimistic insert here — we don't know the chatter row's id /
    // color / display_name until the server resolves the username. The
    // refetch is fast (single query); the form just re-enables after.
    onSuccess: () => qc.invalidateQueries({ queryKey: QK }),
  });

  const inviteM = useMutation({
    mutationFn: () => relay.post<InviteResp>("/admin/chatters/invite", {}),
  });

  const rows = q.data ?? [];

  return (
    <div className="space-y-4">
      <Card>
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-base font-semibold">Your chatters</h2>
          <span className="text-[11px] text-fg-dim">{rows.length} linked</span>
        </div>

        {q.isLoading && <div className="text-fg-dim text-sm">Loading…</div>}
        {q.error && (
          <div className="text-err text-sm">{(q.error as Error).message || "failed"}</div>
        )}
        {!q.isLoading && rows.length === 0 && (
          <div className="text-fg-dim text-sm">
            No chatters linked yet. Invite one below.
          </div>
        )}

        {rows.length > 0 && (
          <div className="overflow-x-auto">
          <table className="w-full min-w-[560px] text-sm">
            <thead>
              <tr className="text-left text-fg-dim text-xs border-b border-border">
                <th className="px-3 py-2 font-medium w-8"></th>
                <th className="px-3 py-2 font-medium">Username</th>
                <th className="px-3 py-2 font-medium">Display name</th>
                <th className="px-3 py-2 font-medium">Mirror employee</th>
                <th className="px-3 py-2 font-medium w-24 text-right">Actions</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.id} className="border-b border-border/40 last:border-0">
                  <td className="px-3 py-2.5">
                    <span
                      className="inline-block w-3 h-3 rounded-full"
                      style={{ background: r.color || "#666" }}
                      aria-hidden
                    />
                  </td>
                  <td className="px-3 py-2.5 font-mono text-xs">{r.username}</td>
                  <td className="px-3 py-2.5">{r.display_name || <span className="text-fg-dim">—</span>}</td>
                  <td className="px-3 py-2.5 text-xs text-fg-dim">
                    {r.employee_id_for_this_owner != null
                      ? `#${r.employee_id_for_this_owner}`
                      : "not yet (auto-created on first action)"}
                  </td>
                  <td className="px-3 py-2.5 text-right whitespace-nowrap">
                    <button
                      type="button"
                      onClick={() => setAccessFor((cur) => (cur === r.id ? null : r.id))}
                      className="text-[11px] text-fg-dim hover:text-accent underline underline-offset-2 mr-3"
                    >
                      {accessFor === r.id ? "Close" : "Access"}
                    </button>
                    <button
                      type="button"
                      onClick={() => unlinkM.mutate(r.id)}
                      className="text-[11px] text-fg-dim hover:text-err underline underline-offset-2"
                      disabled={unlinkM.isPending}
                    >
                      Unlink
                    </button>
                  </td>
                </tr>
              ))}
              {accessFor && rows.some((r) => r.id === accessFor) && (
                <tr>
                  <td colSpan={5} className="px-3 py-3 bg-bg/30 border-b border-border/40">
                    <AccessEditor
                      chatterId={accessFor}
                      onClose={() => setAccessFor(null)}
                    />
                  </td>
                </tr>
              )}
            </tbody>
          </table>
          </div>
        )}
      </Card>

      <InviteCard
        onMint={() => inviteM.mutate()}
        invite={inviteM.data ?? null}
        error={inviteM.error as Error | null}
        pending={inviteM.isPending}
      />

      <LinkCard
        onLink={(username) => linkM.mutate({ username })}
        pending={linkM.isPending}
        error={linkM.error as Error | null}
        ok={linkM.data ?? null}
      />
    </div>
  );
}

function InviteCard({
  onMint, invite, error, pending,
}: {
  onMint: () => void;
  invite: InviteResp | null;
  error: Error | null;
  pending: boolean;
}) {
  const url = invite
    ? `${typeof window !== "undefined" ? window.location.origin : ""}${invite.accept_path}`
    : "";
  const [copied, setCopied] = useState(false);

  async function copy() {
    try {
      await navigator.clipboard.writeText(url);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch { /* clipboard may be blocked — user can still select manually */ }
  }

  return (
    <Card>
      <h3 className="text-sm font-semibold mb-3">Invite a new chatter</h3>
      <p className="text-xs text-fg-dim mb-3">
        Mint a one-shot signup URL. The chatter creates their account
        through that link and gets auto-linked to you on first login.
        Token expires after 24 hours.
      </p>
      {!invite && (
        <Button size="sm" onClick={onMint} disabled={pending}>
          {pending ? "Minting…" : "Mint invite URL"}
        </Button>
      )}
      {invite && (
        <div className="space-y-2">
          <div className="flex items-center gap-2">
            <input
              readOnly
              value={url}
              onFocus={(e) => e.currentTarget.select()}
              className="flex-1 bg-bg border border-border rounded-md px-3 py-1.5 text-xs font-mono focus:outline-none"
            />
            <Button size="sm" onClick={copy}>
              {copied ? "Copied" : "Copy"}
            </Button>
            <Button size="sm" onClick={onMint} disabled={pending}>
              {pending ? "…" : "New"}
            </Button>
          </div>
          <div className="text-[11px] text-fg-dim">
            Expires {new Date(invite.expires_at).toLocaleString()}.
          </div>
        </div>
      )}
      {error && <p className="text-err text-xs mt-2">{error.message}</p>}
    </Card>
  );
}

function LinkCard({
  onLink, pending, error, ok,
}: {
  onLink: (username: string) => void;
  pending: boolean;
  error: Error | null;
  ok: { ok: boolean; username: string; already_linked: boolean } | null;
}) {
  const [name, setName] = useState("");

  const errMsg = error instanceof RelayError && error.status === 404
    ? `No chatter named "${name.trim()}" — send them an invite link instead.`
    : error?.message ?? null;

  return (
    <Card>
      <h3 className="text-sm font-semibold mb-3">Link an existing chatter</h3>
      <p className="text-xs text-fg-dim mb-3">
        Use this when the chatter already has an account through another
        owner. They&apos;ll see your accounts in their picker on next sign-in
        (or instantly if they&apos;re currently online).
      </p>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          const u = name.trim().toLowerCase();
          if (!u) return;
          onLink(u);
        }}
        className="flex items-center gap-2"
      >
        <input
          type="text"
          placeholder="Username"
          value={name}
          onChange={(e) => setName(e.target.value)}
          className="flex-1 bg-bg border border-border rounded-md px-3 py-1.5 text-sm focus:outline-none focus:border-accent"
        />
        <Button size="sm" type="submit" disabled={!name.trim() || pending}>
          {pending ? "…" : "Link"}
        </Button>
      </form>
      {ok && (
        <p className="text-ok text-xs mt-2">
          {ok.already_linked
            ? `@${ok.username} was already linked.`
            : `Linked @${ok.username}.`}
        </p>
      )}
      {errMsg && <p className="text-err text-xs mt-2">{errMsg}</p>}
    </Card>
  );
}

// ── Access editor (Chatter Access, step 6.1) ────────────────────────
//
// Owner-set, per-chatter visibility limits. Two SUBTRACTIVE allowlists:
//   • Limit models — which of THIS owner's accounts the chatter sees.
//   • Limit folders — per allowed account, which vault folders the
//     chatter sees in the picker.
// Both default OFF (= full access). DA-2: an empty restricted set is never
// persisted — "see nothing" is Unlink, not an empty allowlist. So when a
// "Limit …" toggle is ON, at least one selection is required to save.

interface FolderGrant {
  folder_id: FanId;
  folder_name: string | null;
}

interface FolderEntry {
  limit: boolean;
  grants: FolderGrant[];
}

function AccessEditor({ chatterId, onClose }: { chatterId: string; onClose: () => void }) {
  const qc = useQueryClient();
  const accountsQ = useAccounts();
  const ownerAccounts = accountsQ.data?.accounts ?? [];

  const accessQ = useQuery<AccessResp>({
    queryKey: ["admin", "chatters", chatterId, "access"],
    queryFn: () => relay.get<AccessResp>(`/admin/chatters/${chatterId}/access`),
    staleTime: 5_000,
  });

  const [limitModels, setLimitModels] = useState(false);
  const [selectedAccounts, setSelectedAccounts] = useState<Set<string>>(new Set());
  // Per-account folder limit state. Seeded from the server's folder_access.
  const [folderState, setFolderState] = useState<Record<string, FolderEntry>>({});
  const [seededFor, setSeededFor] = useState<string | null>(null);
  const [saveErr, setSaveErr] = useState<string | null>(null);
  const [savedAt, setSavedAt] = useState<number | null>(null);

  // Seed local state once the server access payload lands (re-seed when the
  // editor is opened for a different chatter).
  if (accessQ.data && seededFor !== chatterId) {
    const d = accessQ.data;
    setLimitModels(d.account_ids.length > 0);
    setSelectedAccounts(new Set(d.account_ids));
    const fs: Record<string, FolderEntry> = {};
    for (const [aid, ids] of Object.entries(d.folder_access || {})) {
      fs[aid] = {
        limit: ids.length > 0,
        grants: ids.map((id) => ({ folder_id: id, folder_name: null })),
      };
    }
    setFolderState(fs);
    setSeededFor(chatterId);
  }

  // Accounts the chatter can actually see right now (drives which accounts
  // get a folder-limit control). Limit OFF ⇒ all of this owner's accounts.
  const visibleAccountIds = useMemo(() => {
    if (limitModels) return ownerAccounts.filter((a) => selectedAccounts.has(a.id)).map((a) => a.id);
    return ownerAccounts.map((a) => a.id);
  }, [limitModels, selectedAccounts, ownerAccounts]);

  const saveM = useMutation({
    mutationFn: async () => {
      await relay.put(`/admin/chatters/${chatterId}/account-access`, {
        account_ids: limitModels ? [...selectedAccounts] : [],
      });
      // Folder grants for every owner account, not just the visible ones — an
      // account de-selected from "Limit models" must have its prior folder
      // restriction cleared too (sends []), or stale grants resurrect.
      const visible = new Set(visibleAccountIds);
      for (const a of ownerAccounts) {
        const entry = folderState[a.id];
        await relay.put(`/admin/chatters/${chatterId}/folder-access`, {
          account_id: a.id,
          folders: visible.has(a.id) && entry?.limit ? entry.grants : [],
        });
      }
    },
    onSuccess: () => {
      setSavedAt(Date.now());
      setSaveErr(null);
      qc.invalidateQueries({ queryKey: ["admin", "chatters", chatterId, "access"] });
    },
    onError: (e) => setSaveErr((e as Error).message || "save failed"),
  });

  // DA-2: an ON "Limit models" toggle with zero selections is not savable.
  const accountSelectionInvalid = limitModels && selectedAccounts.size === 0;
  // Same rule per-account for folders.
  const folderSelectionInvalid = visibleAccountIds.some(
    (aid) => folderState[aid]?.limit && (folderState[aid]?.grants.length ?? 0) === 0,
  );
  const cannotSave = accountSelectionInvalid || folderSelectionInvalid;

  // Editing any selection invalidates a prior "Saved." confirmation.
  const setFolderEntry = useCallback((aid: string, next: FolderEntry) => {
    setSavedAt(null);
    setFolderState((prev) => ({ ...prev, [aid]: next }));
  }, []);

  if (accessQ.isLoading) {
    return <div className="text-fg-dim text-xs">Loading access…</div>;
  }
  if (accessQ.error) {
    return <div className="text-err text-xs">{(accessQ.error as Error).message || "failed"}</div>;
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h4 className="text-sm font-semibold">Access limits</h4>
        <button type="button" onClick={onClose} className="text-[11px] text-fg-dim hover:text-fg">✕</button>
      </div>

      <div className="space-y-2">
        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={limitModels}
            onChange={(e) => { setSavedAt(null); setLimitModels(e.target.checked); }}
          />
          <span>Limit which models this chatter sees</span>
        </label>
        <p className="text-[11px] text-fg-dim ml-6">
          Off = sees all of your models. To hide everything, Unlink the chatter instead.
        </p>

        {limitModels && (
          <div className="ml-6 space-y-1.5 mt-2">
            {ownerAccounts.length === 0 && (
              <div className="text-[11px] text-fg-dim">You have no models to grant.</div>
            )}
            {ownerAccounts.map((a) => (
              <label key={a.id} className="flex items-center gap-2 text-xs">
                <input
                  type="checkbox"
                  checked={selectedAccounts.has(a.id)}
                  onChange={(e) => {
                    setSavedAt(null);
                    setSelectedAccounts((prev) => {
                      const next = new Set(prev);
                      if (e.target.checked) next.add(a.id);
                      else next.delete(a.id);
                      return next;
                    });
                  }}
                />
                <span
                  className="inline-block w-2.5 h-2.5 rounded-full"
                  style={{ background: a.color || "#666" }}
                  aria-hidden
                />
                <span>{a.nickname || a.id}</span>
              </label>
            ))}
            {accountSelectionInvalid && (
              <p className="text-err text-[11px]">Pick at least one model, or turn the limit off.</p>
            )}
          </div>
        )}
      </div>

      <div className="space-y-3 border-t border-border/40 pt-3">
        <h5 className="text-xs font-semibold text-fg-dim">Folder limits (per model)</h5>
        {visibleAccountIds.length === 0 && (
          <div className="text-[11px] text-fg-dim">
            {limitModels ? "Select a model above to set folder limits." : "No models available."}
          </div>
        )}
        {visibleAccountIds.map((aid) => {
          const a = ownerAccounts.find((x) => x.id === aid);
          return (
            <FolderLimit
              key={aid}
              accountId={aid}
              accountLabel={a?.nickname || aid}
              entry={folderState[aid] ?? { limit: false, grants: [] }}
              onChange={setFolderEntry}
            />
          );
        })}
      </div>

      <div className="flex items-center gap-3 pt-1">
        <Button size="sm" onClick={() => saveM.mutate()} disabled={cannotSave || saveM.isPending}>
          {saveM.isPending ? "Saving…" : "Save access"}
        </Button>
        {savedAt && !saveM.isPending && <span className="text-ok text-[11px]">Saved.</span>}
        {saveErr && <span className="text-err text-[11px]">{saveErr}</span>}
      </div>
    </div>
  );
}

function FolderLimit({
  accountId, accountLabel, entry, onChange,
}: {
  accountId: string;
  accountLabel: string;
  entry: FolderEntry;
  onChange: (aid: string, next: FolderEntry) => void;
}) {
  // Lazy: only fetch the model's folders when the limit is turned on.
  // Shared paginating fetcher — the grant list must show EVERY folder, and
  // a bare limit=50 fetch under this key would also poison the picker cache.
  const listsQ = useQuery<VaultListsResp>({
    queryKey: ["vault-lists", accountId],
    queryFn: () => fetchAllVaultLists({ accountId }),
    enabled: entry.limit,
    staleTime: 5 * 60 * 1000,
    refetchOnWindowFocus: false,
  });
  // Owner sees ALL custom folders (this fetch is owner-scoped, unfiltered).
  const folders = useMemo(
    () => (listsQ.data?.list ?? []).filter((l) => l.type === "custom"),
    [listsQ.data],
  );
  const selectedIds = useMemo(
    () => new Set(entry.grants.map((g) => g.folder_id)),
    [entry.grants],
  );

  // Backfill folder_name on grants that were seeded from the server with a
  // null name (DA-3 — names must round-trip to the denormalised column).
  useEffect(() => {
    if (!entry.limit || folders.length === 0) return;
    const nameById = new Map(folders.map((l) => [l.id, l.name] as const));
    let changed = false;
    const next = entry.grants.map((g) => {
      if (g.folder_name == null && nameById.has(g.folder_id)) {
        changed = true;
        return { folder_id: g.folder_id, folder_name: nameById.get(g.folder_id)! };
      }
      return g;
    });
    if (changed) onChange(accountId, { limit: entry.limit, grants: next });
  }, [folders, entry.limit, entry.grants, accountId, onChange]);

  function toggleLimit(on: boolean) {
    onChange(accountId, { limit: on, grants: on ? entry.grants : [] });
  }

  function toggleFolder(id: FanId, name: string, on: boolean) {
    const next = on
      ? [...entry.grants.filter((g) => g.folder_id !== id), { folder_id: id, folder_name: name }]
      : entry.grants.filter((g) => g.folder_id !== id);
    onChange(accountId, { limit: entry.limit, grants: next });
  }

  return (
    <div className="rounded-md border border-border/50 px-3 py-2">
      <label className="flex items-center gap-2 text-xs">
        <input type="checkbox" checked={entry.limit} onChange={(e) => toggleLimit(e.target.checked)} />
        <span className="font-medium">{accountLabel}</span>
        <span className="text-fg-dim">— limit folders</span>
      </label>
      {entry.limit && (
        <div className="ml-6 mt-2 space-y-1">
          {listsQ.isLoading && <div className="text-[11px] text-fg-dim">Loading folders…</div>}
          {listsQ.error && (
            <div className="text-err text-[11px]">{(listsQ.error as Error).message || "failed"}</div>
          )}
          {!listsQ.isLoading && folders.length === 0 && !listsQ.error && (
            <div className="text-[11px] text-fg-dim">No custom folders on this model.</div>
          )}
          {folders.map((l) => (
            <label key={l.id} className="flex items-center gap-2 text-[11px]">
              <input
                type="checkbox"
                checked={selectedIds.has(l.id)}
                onChange={(e) => toggleFolder(l.id, l.name, e.target.checked)}
              />
              <span>{l.name}</span>
            </label>
          ))}
          {entry.grants.length === 0 && folders.length > 0 && (
            <p className="text-err text-[11px]">Pick at least one folder, or turn this off.</p>
          )}
        </div>
      )}
    </div>
  );
}
