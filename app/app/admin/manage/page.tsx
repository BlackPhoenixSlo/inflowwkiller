"use client";

/**
 * /admin/manage — founder-only user management UI.
 *
 * Lives at /admin/manage (NOT /admin/users) on purpose: Next file-system
 * routes win over rewrites, so a page at /admin/users would shadow the
 * relay's /admin/users endpoint and the page's own fetch would resolve
 * to its own HTML. /admin/manage sidesteps the collision.
 *
 * Lets the founder:
 *   • View as another user (1h read-only impersonation overlay).
 *   • Grant / revoke individual OF accounts (no SQL-poking required).
 *   • Hard-delete a user with a two-step confirm (type "delete" + admin
 *     password).
 *
 * All three flows require the relay's ADMIN_PASSWORD env var value.
 */

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";

import { relay } from "@/lib/relay";
import { useKeyDraft } from "@/hooks/useKeyDraft";
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

function fmtDate(iso: string): string {
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

type OpenPane = { userId: string; pane: "grant" | "keys" | "delete" | "impersonate" } | null;

export default function AdminUsersPage() {
  const { user: me } = useUser();
  const [rows, setRows] = useState<AdminUserRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState<OpenPane>(null);
  const [refreshing, setRefreshing] = useState(false);

  const refresh = useCallback(async () => {
    setError(null);
    setRefreshing(true);
    try {
      const data = await relay.get<AdminUserRow[]>("/admin/users");
      setRows(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setRefreshing(false);
    }
  }, []);

  useEffect(() => { void refresh(); }, [refresh]);

  const togglePane = (userId: string, pane: "grant" | "keys" | "delete" | "impersonate") =>
    setOpen((cur) =>
      cur && cur.userId === userId && cur.pane === pane
        ? null
        : { userId, pane },
    );

  return (
    <main className="max-w-3xl mx-auto p-6 space-y-6">
      <header className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold">Users</h1>
        <button
          type="button"
          onClick={() => void refresh()}
          disabled={refreshing}
          className="text-sm px-3 py-1.5 rounded-lg border border-fg/10 hover:bg-fg/5 disabled:opacity-50 inline-flex items-center gap-1.5"
        >
          <span className={refreshing ? "inline-block animate-spin" : "inline-block"}>↻</span>
          <span>{refreshing ? "Refreshing…" : "Refresh"}</span>
        </button>
      </header>

      {error && (
        <div className="text-sm text-red-400" role="alert">{error}</div>
      )}

      {rows === null ? (
        <div className="text-sm text-fg-dim">Loading…</div>
      ) : rows.length === 0 ? (
        <div className="text-sm text-fg-dim">No users registered yet.</div>
      ) : (
        <ul className="divide-y divide-fg/10 border border-fg/10 rounded-xl">
          {rows.map((u) => {
            const grantOpen = open?.userId === u.id && open.pane === "grant";
            const keysOpen = open?.userId === u.id && open.pane === "keys";
            const deleteOpen = open?.userId === u.id && open.pane === "delete";
            const impersonateOpen = open?.userId === u.id && open.pane === "impersonate";
            const isSelf = me?.user_id === u.id;
            return (
              <li key={u.id} className="p-4 space-y-3">
                <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1">
                  <div className="font-medium">@{u.username}</div>
                  <div className="text-xs text-fg-dim">
                    {u.account_count} account{u.account_count === 1 ? "" : "s"}
                  </div>
                  <div className="text-xs text-fg-dim">
                    joined {fmtDate(u.created_at)}
                  </div>
                  <div className="text-xs text-fg-dim">
                    last seen {fmtDate(u.last_seen_at)}
                  </div>
                  <div className="ml-auto flex items-center gap-3">
                    <button
                      type="button"
                      className="text-xs text-amber-400 hover:text-amber-300"
                      onClick={() => togglePane(u.id, "impersonate")}
                      disabled={isSelf}
                      title={isSelf ? "You can't impersonate yourself" : ""}
                    >
                      {impersonateOpen ? "Cancel" : "View as"}
                    </button>
                    <button
                      type="button"
                      className="text-xs text-fg-dim hover:text-fg"
                      onClick={() => togglePane(u.id, "grant")}
                    >
                      {grantOpen ? "Cancel" : "Grant access"}
                    </button>
                    <button
                      type="button"
                      className="text-xs text-fg-dim hover:text-fg"
                      onClick={() => togglePane(u.id, "keys")}
                    >
                      {keysOpen ? "Cancel" : "AI keys"}
                    </button>
                    <button
                      type="button"
                      className="text-xs text-red-400 hover:text-red-300"
                      onClick={() => togglePane(u.id, "delete")}
                      disabled={isSelf}
                      title={isSelf ? "You can't delete yourself" : ""}
                    >
                      {deleteOpen ? "Cancel" : "Delete user"}
                    </button>
                  </div>
                </div>
                {impersonateOpen && (
                  <ImpersonateForm
                    user={u}
                    onStarted={() => { setOpen(null); }}
                  />
                )}
                {grantOpen && (
                  <GrantForm user={u} onChanged={() => void refresh()} />
                )}
                {keysOpen && <AgencyKeysForm user={u} />}
                {deleteOpen && (
                  <DeleteForm user={u} onDeleted={() => { setOpen(null); void refresh(); }} />
                )}
              </li>
            );
          })}
        </ul>
      )}
    </main>
  );
}

function GrantForm({ user, onChanged }: { user: AdminUserRow; onChanged: () => void }) {
  const [overview, setOverview] = useState<GrantOverview | null>(null);
  const [adminPassword, setAdminPassword] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const data = await relay.get<GrantOverview>(
        `/admin/users/${encodeURIComponent(user.id)}/accounts`,
      );
      setOverview(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [user.id]);

  useEffect(() => { void load(); }, [load]);

  async function grant(accountId: string) {
    if (!adminPassword) {
      setError("admin password required");
      return;
    }
    setError(null);
    setBusy(`grant:${accountId}`);
    try {
      await relay.post(
        `/admin/users/${encodeURIComponent(user.id)}/accounts`,
        { account_id: accountId, admin_password: adminPassword },
      );
      await load();
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  }

  async function revoke(accountId: string) {
    if (!adminPassword) {
      setError("admin password required");
      return;
    }
    setError(null);
    setBusy(`revoke:${accountId}`);
    try {
      await relay.delete(
        `/admin/users/${encodeURIComponent(user.id)}/accounts/${encodeURIComponent(accountId)}`,
        undefined,
        { admin_password: adminPassword },
      );
      await load();
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="grid gap-3 p-3 rounded-lg bg-fg/[0.02] border border-fg/10">
      <p className="text-sm text-fg-dim">
        Link an OF account to <strong>@{user.username}</strong>. They&apos;ll see it in
        their model switcher on next page load. Revoking just drops the link
        row — the underlying session bytes stay where they are.
      </p>
      <label className="grid gap-1 text-sm">
        <span className="text-fg-dim">Admin password</span>
        <input
          type="password"
          value={adminPassword}
          onChange={(e) => setAdminPassword(e.target.value)}
          autoComplete="off"
          className="px-3 py-2 rounded-lg bg-bg border border-fg/10 focus:outline-none focus:border-fg/30"
        />
      </label>
      {error && <div className="text-sm text-red-400" role="alert">{error}</div>}
      {overview === null ? (
        <div className="text-sm text-fg-dim">Loading accounts…</div>
      ) : (
        <div className="grid gap-3">
          <section>
            <h3 className="text-xs uppercase tracking-wide text-fg-dim mb-1">
              Currently granted ({overview.granted.length})
            </h3>
            {overview.granted.length === 0 ? (
              <div className="text-sm text-fg-dim italic">none</div>
            ) : (
              <ul className="grid gap-1">
                {overview.granted.map((a) => (
                  <li key={a.account_id} className="flex items-center gap-2 text-sm">
                    <AccountDot color={a.color} />
                    <span className="truncate flex-1">{a.nickname || a.account_id}</span>
                    <span className="text-[10px] text-fg-dim font-mono">{a.account_id}</span>
                    <button
                      type="button"
                      className="text-xs text-red-400 hover:text-red-300 disabled:opacity-40"
                      onClick={() => void revoke(a.account_id)}
                      disabled={busy !== null || !adminPassword}
                    >
                      {busy === `revoke:${a.account_id}` ? "…" : "Revoke"}
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </section>
          <section>
            <h3 className="text-xs uppercase tracking-wide text-fg-dim mb-1">
              Available to grant ({overview.available.length})
            </h3>
            {overview.available.length === 0 ? (
              <div className="text-sm text-fg-dim italic">no other accounts on this relay</div>
            ) : (
              <ul className="grid gap-1">
                {overview.available.map((a) => (
                  <li key={a.account_id} className="flex items-center gap-2 text-sm">
                    <AccountDot color={a.color} />
                    <span className="truncate flex-1">{a.nickname || a.account_id}</span>
                    <span className="text-[10px] text-fg-dim font-mono">{a.account_id}</span>
                    <button
                      type="button"
                      className="text-xs text-emerald-400 hover:text-emerald-300 disabled:opacity-40"
                      onClick={() => void grant(a.account_id)}
                      disabled={busy !== null || !adminPassword}
                    >
                      {busy === `grant:${a.account_id}` ? "…" : "Grant"}
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </section>
        </div>
      )}
    </div>
  );
}

function AccountDot({ color }: { color: string | null }) {
  return (
    <span
      className="w-2 h-2 rounded-full shrink-0"
      style={{ background: color || "#666" }}
    />
  );
}

function ImpersonateForm({ user, onStarted }: { user: AdminUserRow; onStarted: () => void }) {
  const { startImpersonating } = useUser();
  const router = useRouter();
  const [adminPassword, setAdminPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!adminPassword || busy) return;
    setError(null);
    setBusy(true);
    try {
      await startImpersonating(user.id, adminPassword);
      onStarted();
      // Land on the home page so the founder sees the impersonated user's
      // dashboard from a clean route — staying on /admin/users would just
      // re-render the same list with the banner above it.
      router.push("/");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form
      onSubmit={onSubmit}
      className="grid gap-3 p-3 rounded-lg bg-amber-500/5 border border-amber-500/30"
    >
      <p className="text-sm text-fg-dim">
        Start a 1-hour read-only session as <strong>@{user.username}</strong>.
        The relay will refuse any mutating request (sends, deletes, edits) until
        you exit. A yellow banner stays pinned to the top so you don&apos;t forget
        you&apos;re viewing as someone else.
      </p>
      <label className="grid gap-1 text-sm">
        <span className="text-fg-dim">Admin password</span>
        <input
          type="password"
          value={adminPassword}
          onChange={(e) => setAdminPassword(e.target.value)}
          autoComplete="off"
          className="px-3 py-2 rounded-lg bg-bg border border-fg/10 focus:outline-none focus:border-fg/30"
        />
      </label>
      {error && <div className="text-sm text-red-400" role="alert">{error}</div>}
      <button
        type="submit"
        disabled={!adminPassword || busy}
        className="py-2 rounded-lg bg-amber-500 text-black font-medium disabled:opacity-30"
      >
        {busy ? "Starting…" : "View as @" + user.username}
      </button>
    </form>
  );
}

function DeleteForm({ user, onDeleted }: { user: AdminUserRow; onDeleted: () => void }) {
  const [confirm, setConfirm] = useState("");
  const [adminPassword, setAdminPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const canSubmit = confirm === "delete" && adminPassword.length > 0 && !busy;

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!canSubmit) return;
    setError(null);
    setBusy(true);
    try {
      await relay.delete(
        `/admin/users/${encodeURIComponent(user.id)}`,
        undefined,
        { confirm, admin_password: adminPassword },
      );
      onDeleted();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={onSubmit} className="grid gap-3 p-3 rounded-lg bg-red-500/5 border border-red-500/20">
      <p className="text-sm text-fg-dim">
        Removes <strong>@{user.username}</strong> and {user.account_count}{" "}
        account link{user.account_count === 1 ? "" : "s"}. The OF account rows themselves are kept.
        This cannot be undone.
      </p>
      <label className="grid gap-1 text-sm">
        <span className="text-fg-dim">Type the word <code className="px-1 bg-fg/10 rounded">delete</code></span>
        <input
          type="text"
          value={confirm}
          onChange={(e) => setConfirm(e.target.value)}
          autoComplete="off"
          className="px-3 py-2 rounded-lg bg-bg border border-fg/10 focus:outline-none focus:border-fg/30"
        />
      </label>
      <label className="grid gap-1 text-sm">
        <span className="text-fg-dim">Admin password</span>
        <input
          type="password"
          value={adminPassword}
          onChange={(e) => setAdminPassword(e.target.value)}
          autoComplete="off"
          className="px-3 py-2 rounded-lg bg-bg border border-fg/10 focus:outline-none focus:border-fg/30"
        />
      </label>
      {error && <div className="text-sm text-red-400" role="alert">{error}</div>}
      <button
        type="submit"
        disabled={!canSubmit}
        className="py-2 rounded-lg bg-red-500 text-white font-medium disabled:opacity-30"
      >
        {busy ? "Deleting…" : "Permanently delete"}
      </button>
    </form>
  );
}

/**
 * AgencyKeysForm — set an agency's LLM keys FOR them.
 *
 * The managed flow: they connect the model, the founder does the rest. The
 * self-serve card at Setup → Your AI keys only ever writes the SIGNED-IN
 * owner's row, and impersonation is read-only, so this is the only way to fix
 * an agency that never pasted one — and the way to do the whole post-upgrade
 * pass from a single screen instead of logging in as each owner.
 *
 * The edit rules are NOT restated here. A secret field is safe to edit exactly
 * one way — placeholder never holds the value, blank means unchanged even after
 * a typed-then-backspaced field, only the clear link sends "" — and useKeyDraft
 * is where that lives, shared with both Setup cards. A third hand-rolled copy
 * would be a third place for the next fix to miss.
 *
 * The founder password is the one thing that is local: it gates the write and
 * is not a key, so it never enters the draft.
 */
function AgencyKeysForm({ user }: { user: AdminUserRow }) {
  const [providers, setProviders] = useState<Record<string, KeyStatus> | null>(null);
  const [adminPassword, setAdminPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoadError(null);
    try {
      const data = await relay.get<{ providers: Record<string, KeyStatus> }>(
        `/admin/users/${encodeURIComponent(user.id)}/llm-keys`,
      );
      setProviders(data.providers);
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : String(err));
    }
  }, [user.id]);

  useEffect(() => { void load(); }, [load]);

  // Thrown, not swallowed: useKeyDraft surfaces a rejected submit as a sticky
  // note and keeps the draft, which is what a missing password should do.
  async function submit(values: Record<string, string>) {
    if (!adminPassword) throw new Error("Admin password required.");
    setBusy(true);
    try {
      await relay.put(`/admin/users/${encodeURIComponent(user.id)}/llm-keys`, {
        admin_password: adminPassword,
        providers: values,
      });
      await load();
    } finally {
      setBusy(false);
    }
  }

  const { draft, dirty, setField, save, clear, note } = useKeyDraft(submit, "Saved.");

  return (
    <div className="rounded-lg border border-fg/10 p-3 space-y-3">
      <p className="text-xs text-fg-dim">
        Keys @{user.username}&apos;s models bill. Leave a field blank to keep the
        stored one.
      </p>
      {loadError && <div className="text-xs text-red-400" role="alert">{loadError}</div>}
      {providers === null ? (
        <div className="text-xs text-fg-dim">Loading…</div>
      ) : (
        Object.entries(providers).map(([name, k]) => (
          <div key={name} className="flex items-center gap-2">
            <label className="w-24 text-xs">{name}</label>
            <input
              type="password"
              autoComplete="off"
              value={draft[name] ?? ""}
              placeholder={k.set ? `${k.hint} — blank keeps it` : "not set"}
              onChange={(e) => setField(name, e.target.value)}
              className="flex-1 rounded border border-fg/20 bg-transparent px-2 py-1 text-xs"
            />
            {k.set && (
              <button
                type="button"
                disabled={busy}
                className="text-[11px] text-red-400 hover:text-red-300 disabled:opacity-40"
                onClick={() => {
                  // Someone ELSE's credential: every automation on every account
                  // they own fails closed the moment this lands, and they get no
                  // notification. Same beat the owner's own card takes.
                  if (window.confirm(`Remove @${user.username}'s ${name} key? Their models stop replying until one is added.`)) {
                    void clear(name);
                  }
                }}
              >
                clear
              </button>
            )}
          </div>
        ))
      )}
      <div className="flex items-center gap-2">
        <input
          type="password"
          autoComplete="off"
          value={adminPassword}
          placeholder="admin password"
          onChange={(e) => setAdminPassword(e.target.value)}
          className="flex-1 rounded border border-fg/20 bg-transparent px-2 py-1 text-xs"
        />
        <button
          type="button"
          disabled={busy || !dirty}
          onClick={() => void save()}
          className="rounded bg-fg/10 px-3 py-1 text-xs hover:bg-fg/20 disabled:opacity-40"
        >
          {busy ? "Saving…" : "Save keys"}
        </button>
        {note && <span className="text-xs text-fg-dim">{note}</span>}
      </div>
    </div>
  );
}

interface KeyStatus {
  set: boolean;
  hint: string;
}
