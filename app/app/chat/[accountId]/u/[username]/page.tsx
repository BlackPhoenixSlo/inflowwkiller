"use client";

/**
 * /chat/[accountId]/u/[username] — username → user-id redirector.
 *
 * Notification toasts/dropdown rows that came in via OF's SSE `toasts`
 * channel often have only the actor's username (extracted from the HTML
 * blob) — no numeric id, so we can't link straight to the canonical
 * /chat/<accountId>/<fanId> page. This page does the one-shot lookup
 * via the relay and replaces itself with the right URL.
 */

import { use, useEffect, useState } from "react";

import { relay } from "@/lib/relay";

interface MinUser {
  id?: number | string;
  username?: string;
  name?: string;
}

export default function ChatByUsernamePage({
  params,
}: {
  params: Promise<{ accountId: string; username: string }>;
}) {
  const { accountId, username } = use(params);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const u = await relay.get<MinUser>(
          `/api/of/v2/users/${encodeURIComponent(username)}`,
          { accountId },
        );
        if (cancelled) return;
        const id = u?.id;
        if (id == null) {
          setError(`Couldn't resolve @${username} on this account.`);
          return;
        }
        // Replace so the back button skips this redirect frame.
        window.location.replace(
          `/chat/${encodeURIComponent(accountId)}/${encodeURIComponent(String(id))}`,
        );
      } catch (e) {
        if (cancelled) return;
        setError((e as Error)?.message || `Lookup failed for @${username}.`);
      }
    })();
    return () => { cancelled = true; };
  }, [accountId, username]);

  return (
    <div className="min-h-screen grid place-items-center p-6 text-fg-dim">
      {error ? (
        <div className="max-w-md text-center text-sm">
          <div className="text-fg mb-2">Couldn't open chat</div>
          <div>{error}</div>
        </div>
      ) : (
        <div className="text-sm">Opening chat with @{username}…</div>
      )}
    </div>
  );
}
