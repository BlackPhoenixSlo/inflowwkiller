"use client";

/**
 * EditRawJsonButton + JsonConfigModal — an in-app raw-JSON editor for the
 * per-account config blobs. Each of the five config surfaces stores its
 * settings as a JSON object behind an identical GET/PUT pair
 * (GET /admin/<x>-config?account_id=… → { config }, PUT /admin/<x>-config
 * with { account_id, config }). This modal loads that blob, lets an owner
 * hand-edit it, validates with JSON.parse (must be a JSON object) before
 * saving, and invalidates the surface's React Query key so the normal
 * typed UI reflects the change.
 *
 * Validation mirrors RuleEditor's raw-JSON escape hatch: parse, require an
 * object (not array/scalar/null), surface the parser's message inline.
 */

import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { relay, type RelayContext } from "@/lib/relay";

const BG_CTX: RelayContext = { priority: "background" };

export type ConfigSurface =
  | "account-config"
  | "ppv-library-config"
  | "webhook-config"
  | "style-config"
  | "autoreply-config"
  | "nudge-config"
  | "ai-chatter-config"
  | "tip-reward-config";

/** `queryKey` is the React-Query key the surface's typed hook reads under, so a
 *  raw save invalidates the RIGHT cache and the typed UI refreshes. Most hooks
 *  key `[<surface>, accountId]`, but a few (AI Seller) nest an extra segment. */
const SURFACES: Record<
  ConfigSurface,
  { label: string; path: string; queryKey: (accountId: string) => unknown[] }
> = {
  "account-config": { label: "Brain / account config", path: "/admin/account-config", queryKey: (a) => ["account-config", a] },
  "ppv-library-config": { label: "PPV Library config", path: "/admin/ppv-library-config", queryKey: (a) => ["ppv-library-config", a] },
  "webhook-config": { label: "Instant-reply (webhook) config", path: "/admin/webhook-config", queryKey: (a) => ["webhook-config", a] },
  "style-config": { label: "Style config", path: "/admin/style-config", queryKey: (a) => ["style-config", a] },
  "autoreply-config": { label: "Auto-convo (autoreply) config", path: "/admin/autoreply-config", queryKey: (a) => ["autoreply-config", a] },
  "nudge-config": { label: "Nudge online config", path: "/admin/nudge-config", queryKey: (a) => ["nudge-config", a] },
  "ai-chatter-config": { label: "AI Seller (ai-chatter) config", path: "/admin/ai-chatter-config", queryKey: (a) => ["ai-chatter", "config", a] },
  "tip-reward-config": { label: "Tip Reward config", path: "/admin/tip-reward-config", queryKey: (a) => ["tip-reward-config", a] },
};

export function EditRawJsonButton({
  surface,
  accountId,
  className,
  label = "{ } Edit raw JSON",
}: {
  surface: ConfigSurface;
  accountId: string | null;
  className?: string;
  label?: string;
}) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button
        type="button"
        disabled={!accountId}
        onClick={() => setOpen(true)}
        className={
          className ??
          "text-[11px] text-fg-dim hover:text-fg underline underline-offset-2 disabled:opacity-40 disabled:no-underline"
        }
        title={accountId ? "Edit the raw JSON for this config" : "Pick an account first"}
      >
        {label}
      </button>
      {open && accountId && (
        <JsonConfigModal
          surface={surface}
          accountId={accountId}
          onClose={() => setOpen(false)}
        />
      )}
    </>
  );
}

function JsonConfigModal({
  surface,
  accountId,
  onClose,
}: {
  surface: ConfigSurface;
  accountId: string;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const { label, path, queryKey } = SURFACES[surface];
  const [text, setText] = useState<string>("");
  const [err, setErr] = useState<string | null>(null);
  const [dirty, setDirty] = useState(false);

  const q = useQuery<{ config: unknown }>({
    queryKey: ["raw-json-config", surface, accountId],
    queryFn: () =>
      relay.get<{ config: unknown }>(
        `${path}?account_id=${encodeURIComponent(accountId)}`,
        BG_CTX,
      ),
    staleTime: 0,
    refetchOnWindowFocus: false,
  });

  // Seed the textarea from the loaded config, but stop overwriting once the
  // user starts editing so an in-flight refetch can't stomp their changes.
  useEffect(() => {
    if (q.data && !dirty) {
      setText(JSON.stringify(q.data.config ?? {}, null, 2));
    }
  }, [q.data, dirty]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  const save = useMutation({
    mutationFn: (config: unknown) =>
      relay.put(path, { account_id: accountId, config }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: queryKey(accountId) });
      qc.invalidateQueries({ queryKey: ["raw-json-config", surface, accountId] });
      onClose();
    },
    onError: (e: Error) => setErr(e.message || "Save failed"),
  });

  function parseOrError(): Record<string, unknown> | null {
    const t = text.trim();
    let parsed: unknown;
    try {
      parsed = JSON.parse(t || "{}");
    } catch (e) {
      setErr(`Invalid JSON: ${(e as Error).message}`);
      return null;
    }
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
      setErr('Config must be a JSON object, e.g. { "enabled": true }');
      return null;
    }
    return parsed as Record<string, unknown>;
  }

  function onSave() {
    setErr(null);
    const parsed = parseOrError();
    if (parsed) save.mutate(parsed);
  }

  function reformat() {
    setErr(null);
    const parsed = parseOrError();
    if (parsed) setText(JSON.stringify(parsed, null, 2));
  }

  return (
    <div
      className="fixed inset-0 z-50 bg-black/50 grid place-items-center p-4"
      onClick={onClose}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="w-full max-w-[820px] max-h-[90vh] flex flex-col bg-panel border border-border rounded-xl shadow-2xl"
      >
        <header className="px-4 py-3 border-b border-border flex items-start justify-between gap-3">
          <div className="min-w-0">
            <h2 className="text-sm font-semibold">Edit raw JSON — {label}</h2>
            <p className="text-[11px] text-fg-dim truncate">
              acct {accountId} · PUT {path}
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="text-fg-dim hover:text-fg text-lg leading-none shrink-0"
            title="Close"
          >
            ×
          </button>
        </header>

        <div className="flex-1 overflow-y-auto p-4 min-h-0">
          {q.isLoading ? (
            <div className="text-fg-dim text-sm py-10 text-center">
              Loading current config…
            </div>
          ) : q.isError ? (
            <div className="text-red-400 text-sm py-10 text-center">
              Couldn&apos;t load this config.
            </div>
          ) : (
            <textarea
              value={text}
              onChange={(e) => {
                setDirty(true);
                setText(e.target.value);
                if (err) setErr(null);
              }}
              spellCheck={false}
              className="w-full h-[52vh] rounded-lg bg-bg-elev-1 border border-border text-xs font-mono px-3 py-2 text-fg focus:outline-none focus:ring-2 focus:ring-accent/40 resize-none"
              placeholder="{ }"
            />
          )}
          {err && (
            <div className="mt-2 text-xs text-red-400 whitespace-pre-wrap">{err}</div>
          )}
        </div>

        <footer className="px-4 py-3 border-t border-border flex items-center justify-between gap-2">
          <button
            type="button"
            onClick={reformat}
            disabled={q.isLoading || q.isError}
            className="text-[11px] text-fg-dim hover:text-fg underline underline-offset-2 disabled:opacity-40"
          >
            Reformat
          </button>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={onClose}
              className="text-xs px-3 py-1.5 rounded-lg border border-border text-fg-dim hover:text-fg"
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={onSave}
              disabled={save.isPending || q.isLoading || q.isError}
              className="text-xs px-3 py-1.5 rounded-lg bg-accent text-white disabled:opacity-50"
            >
              {save.isPending ? "Saving…" : "Save"}
            </button>
          </div>
        </footer>
      </div>
    </div>
  );
}
