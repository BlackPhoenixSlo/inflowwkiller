"use client";

/**
 * /chatter-login — sign-in surface for the second principal.
 *
 * Two modes:
 *   • With `?token=...` (invite link from an owner) → register form.
 *     If a User is currently signed in on this browser we explicitly
 *     log them out FIRST, so the new chatter cookie isn't shadowed by
 *     the owner cookie (AuthGate gives User precedence — would leave
 *     the chatter "registered but invisible" otherwise).
 *   • Without a token → login form for returning chatters.
 *
 * On success router.replace("/") so the app shell mounts under the
 * fresh chatter principal.
 *
 * Path is intentionally NOT /chatter — that would shadow the relay
 * rewrite (memory: feedback_next_page_relay_collision).
 */

import { useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { useQuery } from "@tanstack/react-query";

import { useChatter } from "@/contexts/ChatterContext";
import { useUser } from "@/contexts/UserContext";

interface InvitePreview {
  username: string;        // owner who issued the invite
  expires_at: string;
  used: boolean;
}

export default function ChatterLoginPage() {
  const router = useRouter();
  const sp = useSearchParams();
  const token = sp.get("token") || "";
  const { chatter, login, register } = useChatter();
  const { user, clearUserLocally } = useUser();

  const isRegister = !!token;

  // Peek at the invite (read-only — doesn't consume) so the user knows
  // which owner is inviting them and that the link is still valid.
  const invitePreview = useQuery<InvitePreview | null>({
    queryKey: ["chatter", "invite-preview", token],
    queryFn: async () => {
      if (!token) return null;
      const r = await fetch(`/chatter/invite/${encodeURIComponent(token)}`, {
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      });
      if (!r.ok) return null;
      return (await r.json()) as InvitePreview;
    },
    enabled: !!token,
    staleTime: 60_000,
  });

  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Already signed in as a chatter? Bounce straight to the app.
  // Owner signed in (with no token in URL) → also bounce: they probably
  // just clicked an old bookmark and there's nothing to do here.
  useEffect(() => {
    if (chatter) router.replace("/");
    else if (user && !token) router.replace("/");
  }, [chatter, user, token, router]);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (busy) return;
    setError(null);
    setBusy(true);
    try {
      const u = username.trim().toLowerCase();
      if (isRegister) {
        // Owner signed in on the same browser? Drop the User cookie
        // first so AuthGate doesn't keep showing the owner's view after
        // the chatter is created. Local-only clear — the chatter
        // register call sets the chatter cookie a moment later.
        if (user) clearUserLocally();
        await register(u, password, token, displayName.trim() || undefined);
      } else {
        await login(u, password);
      }
      // Hard navigation, not router.replace: the chatter cookie was
      // just set on the response and React Query has the new principal
      // seeded. A full page load guarantees the new cookie ships on
      // every subsequent fetch and re-mounts every provider from
      // scratch — what the user used to have to do manually with
      // "click refresh." A client-side transition was racing the
      // observer notifications and occasionally landed on AuthGate
      // while it still saw chatter=null.
      window.location.replace("/");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setBusy(false);
    }
    // Note: no finally clearing busy — on success we hard-navigate so
    // the form unmounts before the next event loop tick anyway.
  }

  return (
    <main className="min-h-screen flex items-center justify-center p-6 bg-bg text-fg">
      <div className="w-full max-w-md grid gap-6">
        <header className="space-y-2 text-center">
          <h1 className="text-3xl font-semibold tracking-tight">Chatterly</h1>
          <p className="text-sm text-fg-dim">
            {isRegister ? "Set up your chatter account" : "Sign in as a chatter"}
          </p>
        </header>

        {/* Invite context: who invited you, and is the link still good. */}
        {isRegister && invitePreview.data && (
          <div className={
            invitePreview.data.used
              ? "rounded-xl border border-err/30 bg-err/5 p-4 text-sm"
              : "rounded-xl border border-fg/10 bg-bg-elev-1 p-4 text-sm"
          }>
            {invitePreview.data.used ? (
              <>
                <div className="font-semibold text-err mb-1">This invite link is no longer valid.</div>
                <div className="text-fg-dim text-xs">
                  Ask <span className="font-mono">@{invitePreview.data.username}</span> for a fresh link.
                </div>
              </>
            ) : (
              <>
                <div className="mb-1">
                  <span className="font-mono">@{invitePreview.data.username}</span> invited you to chatter.
                </div>
                <div className="text-fg-dim text-xs">
                  Link expires {new Date(invitePreview.data.expires_at).toLocaleString()}.
                </div>
              </>
            )}
          </div>
        )}
        {isRegister && invitePreview.isError && (
          <div className="rounded-xl border border-err/30 bg-err/5 p-4 text-sm">
            <div className="font-semibold text-err mb-1">Invite token not found.</div>
            <div className="text-fg-dim text-xs">
              Double-check the URL or ask the owner for a fresh invite link.
            </div>
          </div>
        )}

        {/* Owner cookie warning: heading into the register submit will
         *  sign them out of their owner account on this browser. */}
        {isRegister && user && (
          <div className="rounded-xl border border-fg/10 bg-bg-elev-1 p-4 text-xs text-fg-dim">
            You&apos;re currently signed in as <span className="font-mono text-fg">@{user.username}</span>.
            Creating a chatter account will sign you out and switch this
            browser to the new chatter session.
          </div>
        )}

        <form
          onSubmit={onSubmit}
          className="grid gap-3 rounded-2xl border border-fg/10 p-5"
        >
          <label className="grid gap-1 text-sm">
            <span className="text-fg-dim">Username</span>
            <input
              type="text"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              autoComplete="username"
              required
              minLength={3}
              maxLength={32}
              pattern="[a-zA-Z0-9_.\-]{3,32}"
              className="px-3 py-2 rounded-lg bg-bg border border-fg/10 focus:outline-none focus:border-fg/30"
            />
          </label>

          <label className="grid gap-1 text-sm">
            <span className="text-fg-dim">Password</span>
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete={isRegister ? "new-password" : "current-password"}
              required
              minLength={6}
              className="px-3 py-2 rounded-lg bg-bg border border-fg/10 focus:outline-none focus:border-fg/30"
            />
          </label>

          {isRegister && (
            <label className="grid gap-1 text-sm">
              <span className="text-fg-dim">Display name (optional)</span>
              <input
                type="text"
                value={displayName}
                onChange={(e) => setDisplayName(e.target.value)}
                maxLength={64}
                className="px-3 py-2 rounded-lg bg-bg border border-fg/10 focus:outline-none focus:border-fg/30"
              />
            </label>
          )}

          {error && (
            <div className="text-sm text-red-400" role="alert">
              {error}
            </div>
          )}

          <button
            type="submit"
            disabled={busy || (isRegister && invitePreview.data?.used === true)}
            className="mt-1 py-2 rounded-lg bg-fg text-bg font-medium disabled:opacity-50"
          >
            {busy ? "…" : isRegister ? "Create chatter account" : "Sign in"}
          </button>

          {isRegister && (
            <p className="text-xs text-fg-dim">
              Username: 3–32 chars, lowercase a–z 0–9 . _ -. Password: 6+ chars.
            </p>
          )}
        </form>

        {/* Returning chatter? Owner signing in? Pointer to the other surface. */}
        {!isRegister && (
          <div className="text-center text-xs text-fg-dim">
            Owner sign-in? <a href="/login" className="underline hover:text-fg">Use the main login</a>.
          </div>
        )}
      </div>
    </main>
  );
}
