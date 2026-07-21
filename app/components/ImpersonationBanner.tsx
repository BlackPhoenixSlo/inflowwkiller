"use client";

/**
 * ImpersonationBanner — sticky strip mounted above TopNav when the
 * founder is viewing the app as another user via /admin/impersonate.
 *
 * Reads from UserContext (user.impersonating). Hidden entirely when the
 * overlay isn't active, so the cost on every other render is one truthy
 * check. The relay 403s mutating requests while impersonating, so this
 * banner is the user's visual cue for why a send button would suddenly
 * fail.
 */

import { useState } from "react";

import { useUser } from "@/contexts/UserContext";

export function ImpersonationBanner() {
  const { user, stopImpersonating } = useUser();
  const [busy, setBusy] = useState(false);

  if (!user?.impersonating) return null;

  async function onExit() {
    setBusy(true);
    try {
      await stopImpersonating();
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="relative sm:sticky top-0 z-40 bg-amber-500 text-black text-xs sm:text-sm">
      <div className="max-w-7xl mx-auto px-3 lg:px-6 py-2 flex items-center gap-3 flex-wrap">
        <span className="font-semibold">Viewing as @{user.username}</span>
        <span className="hidden sm:inline opacity-80">
          (you: @{user.impersonating.real_username}) — read-only
        </span>
        <button
          type="button"
          onClick={() => void onExit()}
          disabled={busy}
          className="ml-auto px-3 py-2 sm:px-2.5 sm:py-1 min-h-10 sm:min-h-0 rounded bg-black/15 hover:bg-black/25 font-medium disabled:opacity-50"
        >
          {busy ? "Exiting…" : "Exit impersonation"}
        </button>
      </div>
    </div>
  );
}
