"use client";

/**
 * Landing — single-page entry for signed-out friends.
 *
 * Sections:
 *   • Hero with the "very fast" pitch + load numbers.
 *   • What it does (three bullets).
 *   • Transparency line on cost ("I pay the bills, here is the repo").
 *   • Sign in / register form with a single toggle.
 *
 * On submit the form calls UserContext.login() or register(), which
 * sets the session cookie. We then redirect to `?next=` if present, else
 * `/inbox`.
 */

import { useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";

import { useUser } from "@/contexts/UserContext";
import { useChatter } from "@/contexts/ChatterContext";

type Mode = "login" | "register";

export default function Landing() {
  const { login, register } = useUser();
  const { login: chatterLogin } = useChatter();
  const router = useRouter();
  const sp = useSearchParams();
  const nextHref = sp.get("next") || "/inbox";

  const [mode, setMode] = useState<Mode>("login");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (busy) return;
    setError(null);
    setBusy(true);
    try {
      const u = username.trim().toLowerCase();
      let signedInAsChatter = false;
      if (mode === "login") {
        // Unified login: try owner first, then chatter. Whichever
        // principal owns this username succeeds; the other returns 401
        // and we silently fall through. We keep the chatter error
        // message on full failure since "wrong username or password"
        // is identical either way and the chatter call ran last.
        try {
          await login(u, password);
        } catch (err) {
          // Only fall back on auth failures, not network / 5xx errors —
          // those would silently disappear into the second attempt.
          const msg = err instanceof Error ? err.message : String(err);
          const looksLikeAuthFail = /invalid|password|401|403/i.test(msg);
          if (!looksLikeAuthFail) throw err;
          await chatterLogin(u, password);
          signedInAsChatter = true;
        }
      } else {
        // Owner registration only. Chatter registration requires an
        // owner-issued invite token (security) and lives on
        // /chatter-login?token=… exclusively.
        await register(u, password);
      }
      // Chatter sign-in uses a hard navigation so the new cookie ships
      // on every subsequent fetch and the providers re-mount with the
      // chatter principal cleanly. Owner sign-in goes through the
      // existing client-side flow which has worked since launch.
      if (signedInAsChatter) {
        window.location.replace(nextHref);
      } else {
        router.replace(nextHref);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="min-h-screen flex items-center justify-center p-6 bg-bg text-fg">
      <div className="w-full max-w-3xl grid gap-6 sm:gap-10">
        <header className="space-y-3 text-center">
          <h1 className="text-3xl sm:text-5xl font-bold tracking-tight">Fastt</h1>
          <p className="text-xl text-fg-dim">Very fast.</p>
          <p className="hidden sm:block text-sm text-fg-dim mx-auto max-w-md">
            Conversations open instantly. Vault scrolls without spinners. Built for chatters
            who lose minutes per shift waiting on the OF dashboard.
          </p>
        </header>

        <section className="hidden sm:grid sm:grid-cols-3 gap-4 text-sm">
          <div className="rounded-xl border border-fg/10 p-4">
            <div className="font-semibold mb-1">Inbox</div>
            <div className="text-fg-dim">
              Unified across every model. Open a chat, the history is already there.
            </div>
          </div>
          <div className="rounded-xl border border-fg/10 p-4">
            <div className="font-semibold mb-1">Vault</div>
            <div className="text-fg-dim">
              Scroll without round-trips. Send media in one click, previews handled.
            </div>
          </div>
          <div className="rounded-xl border border-fg/10 p-4">
            <div className="font-semibold mb-1">Automations</div>
            <div className="text-fg-dim">
              Mass messages, welcome flows, follow-ups. Your rules, your accounts.
            </div>
          </div>
        </section>

        <form
          onSubmit={onSubmit}
          className="grid gap-3 max-w-sm w-full mx-auto rounded-2xl border border-fg/10 p-5"
        >
          <div className="flex gap-2 text-sm font-medium">
            <button
              type="button"
              onClick={() => { setMode("login"); setError(null); }}
              className={`flex-1 py-2 rounded-lg ${mode === "login" ? "bg-fg/10" : "hover:bg-fg/5"}`}
            >
              Sign in
            </button>
            <button
              type="button"
              onClick={() => { setMode("register"); setError(null); }}
              className={`flex-1 py-2 rounded-lg ${mode === "register" ? "bg-fg/10" : "hover:bg-fg/5"}`}
            >
              Register
            </button>
          </div>

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
              className="px-3 py-2 text-base md:text-sm rounded-lg bg-bg border border-fg/10 focus:outline-none focus:border-fg/30"
            />
          </label>

          <label className="grid gap-1 text-sm">
            <span className="text-fg-dim">Password</span>
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete={mode === "login" ? "current-password" : "new-password"}
              required
              minLength={6}
              className="px-3 py-2 text-base md:text-sm rounded-lg bg-bg border border-fg/10 focus:outline-none focus:border-fg/30"
            />
          </label>

          {error && (
            <div className="text-sm text-red-400" role="alert">
              {error}
            </div>
          )}

          <button
            type="submit"
            disabled={busy}
            className="mt-1 py-2 rounded-lg bg-fg text-bg font-medium disabled:opacity-50"
          >
            {busy ? "…" : mode === "login" ? "Sign in" : "Create account"}
          </button>

          {mode === "register" && (
            <p className="text-xs text-fg-dim">
              Username: 3–32 chars, lowercase a–z 0–9 . _ -. Password: 6+ chars.
            </p>
          )}
        </form>

        {/* Desktop app. Gated behind sm: like the blurb below — an installer
            is no use on a phone, and offering one there is just a dead end. */}
        <a
          href="/download"
          className="hidden sm:flex mx-auto max-w-sm w-full items-center gap-3 rounded-xl border border-fg/10 px-4 py-3 hover:border-fg/30 transition-colors"
        >
          <span aria-hidden="true" className="text-fg-dim text-lg leading-none">&#8595;</span>
          <span className="grid gap-0.5">
            <span className="text-sm font-medium">Get the desktop app</span>
            <span className="text-xs text-fg-dim">
              Mac &amp; Windows &mdash; keeps your inbox live in the background.
            </span>
          </span>
        </a>

        <section className="hidden sm:block space-y-3 text-sm">
          <p className="text-fg-dim text-center">
            I host this for my friends.{" "}
            <a
              href="https://github.com/BlackPhoenixSlo/inflowwkiller"
              target="_blank"
              rel="noopener noreferrer"
              className="underline underline-offset-2 hover:text-fg"
            >
              The code is on GitHub
            </a>{" "}
            — deploy it yourself on Hostinger in one paste:
          </p>
          <div className="bg-bg rounded-lg border border-fg/10 px-4 py-3 font-mono text-xs text-fg-dim overflow-x-auto whitespace-nowrap">
            bash &lt;(curl -fsSL https://raw.githubusercontent.com/BlackPhoenixSlo/inflowwkiller/main/scripts/deploy-here.sh)
          </div>
        </section>
      </div>
    </main>
  );
}
