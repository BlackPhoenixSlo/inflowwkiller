"use client";

/**
 * ScopeSwitcher — dropdown for "all models" + one entry per session-bearing
 * account. Drives ScopeContext, which in turn drives the chat-list
 * fan-out, the SSE filter, and the X-Account-Id header.
 *
 * Per-row UI is two-target:
 *   • checkbox  → toggles inclusion in the all-models aggregate
 *                 (does NOT swap scope; pure include-set edit).
 *   • name area → setScope({kind:"model", accountId}) (existing behavior).
 *
 * Always visible in TopNav — the per-model include toggles need a
 * persistent home so the user can prune models from the aggregate at any
 * time without first hunting through inbox-settings to re-enable a chip.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";

import { relay } from "@/lib/relay";
import { useScope } from "@/contexts/ScopeContext";
import { useActiveAccounts } from "@/hooks/useAccounts";
import { useAllModelsInclude } from "@/hooks/useAllModelsInclude";
import { useUser } from "@/contexts/UserContext";
import { useChatter } from "@/contexts/ChatterContext";
import { cn } from "@/lib/utils";

export default function ScopeSwitcher() {
  const { scope, setScope } = useScope();
  const accounts = useActiveAccounts();
  const { isIncluded, toggle, includeAll, excludeAll } = useAllModelsInclude();
  const { user } = useUser();
  const { chatter, accounts: chatterAccounts } = useChatter();
  // Owner-grouping mode: only fires when a chatter principal is driving
  // and the User cookie is absent. The dropdown then renders a header
  // row per owner so a chatter linked to @alice and @bob can tell which
  // models belong to whom without inspecting nicknames.
  const groupByOwner = !user && !!chatter;
  // Map account_id → owner_username so we can render group dividers
  // without doing an O(owners × accounts) join in the render loop.
  const accountOwner = useMemo(() => {
    if (!groupByOwner) return null;
    const m = new Map<string, { id: string; username: string }>();
    for (const a of chatterAccounts) {
      m.set(a.account_id, { id: a.owner_id, username: a.owner_username });
    }
    return m;
  }, [groupByOwner, chatterAccounts]);
  const [open, setOpen] = useState(false);
  const wrapRef = useRef<HTMLDivElement | null>(null);

  // Per-model color editing. Every principal may recolor a model — owners
  // AND chatters (PATCH /admin/accounts/{id} is carved out for chatter
  // sessions and the handler forces it to color-only). The swatch is
  // read-only only for a truly anonymous viewer (no user, no chatter).
  const qc = useQueryClient();
  const canEditColor = !!user || !!chatter;
  const recolorM = useMutation({
    mutationFn: ({ id, color }: { id: string; color: string }) =>
      relay.patch<unknown>(`/admin/accounts/${encodeURIComponent(id)}`, { color }),
    // Owners read accounts from ["accounts", ...]; chatters from
    // ["chatter","self","accounts"]. Invalidate both so the new color
    // propagates to the roster dot regardless of which principal is driving.
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["accounts"] });
      void qc.invalidateQueries({ queryKey: ["chatter", "self", "accounts"] });
    },
  });

  useEffect(() => {
    if (!open) return;
    const onClick = (e: MouseEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) setOpen(false);
    };
    window.addEventListener("pointerdown", onClick);
    return () => window.removeEventListener("pointerdown", onClick);
  }, [open]);

  // Refetch the roster every time the picker opens, so a model captured
  // in another session shows up the moment you look for it instead of
  // waiting out staleTime (the persisted cache otherwise holds until
  // then). Both principal keys, same as the recolor invalidation above.
  useEffect(() => {
    if (!open) return;
    void qc.invalidateQueries({ queryKey: ["accounts"] });
    void qc.invalidateQueries({ queryKey: ["chatter", "self", "accounts"] });
  }, [open, qc]);

  // Master-checkbox state for the "All models" row: checked when every
  // listed model is in the aggregate, indeterminate when only some are.
  const allIncluded = accounts.every((a) => isIncluded(a.id));
  const noneIncluded = accounts.length > 0 && accounts.every((a) => !isIncluded(a.id));

  const currentLabel =
    scope.kind === "all"
      ? "all models"
      : accounts.find((a) => a.id === scope.accountId)?.nickname || scope.accountId;
  const currentColor =
    scope.kind === "all"
      ? "#a78bfa"
      : accounts.find((a) => a.id === scope.accountId)?.color || "#666";

  return (
    <div ref={wrapRef} className="relative min-w-0 lg:shrink-0">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex items-center gap-2 px-2 lg:px-3 py-1.5 rounded-lg text-sm bg-bg-elev-1 hover:bg-bg-elev-2 border border-border whitespace-nowrap max-w-[6rem] sm:max-w-[8rem] lg:max-w-none"
      >
        <span
          className="w-2 h-2 rounded-full shrink-0"
          style={{ background: currentColor }}
        />
        <span className="text-xs truncate">{currentLabel}</span>
      </button>
      {open && (
        <>
        {/* Phone/tablet backdrop — the panel is a fixed sheet below lg, so
            give it a tap-to-close surface. Sits under the panel's z-40 and
            never renders at lg, where the panel is an anchored dropdown. */}
        <div
          className="lg:hidden fixed top-0 left-0 w-screen h-dvh z-30"
          onClick={() => setOpen(false)}
          aria-hidden
        />
        <div className="fixed left-2 right-2 top-14 w-auto max-h-[70vh] overflow-y-auto bg-panel border border-border rounded-lg shadow-lg z-40 lg:absolute lg:inset-auto lg:top-full lg:right-0 lg:left-auto lg:mt-1 lg:w-64 lg:max-h-none lg:overflow-hidden lg:overflow-y-hidden">
          <button
            type="button"
            onClick={() => setOpen(false)}
            className="lg:hidden ml-auto w-11 h-11 grid place-items-center text-lg leading-none text-fg-dim hover:text-fg"
            aria-label="Close model picker"
          >
            ✕
          </button>
          {/* "All models" row — master checkbox checks/unchecks every model
              at once (indeterminate when the aggregate is partial); the name
              area swaps scope, same two-target layout as the per-model rows. */}
          <div
            className={cn(
              "w-full flex items-center gap-2 text-sm hover:bg-bg-elev-1",
              scope.kind === "all" && "bg-bg-elev-1/60",
            )}
          >
            <label
              className="pl-3 py-2 flex items-center cursor-pointer select-none"
              title={
                allIncluded
                  ? "Uncheck to exclude every model from the aggregate"
                  : "Check to include every model in the aggregate"
              }
            >
              <input
                type="checkbox"
                checked={allIncluded}
                ref={(el) => {
                  if (el) el.indeterminate = !allIncluded && !noneIncluded;
                }}
                onChange={() =>
                  allIncluded ? excludeAll(accounts.map((a) => a.id)) : includeAll()
                }
                className="w-3.5 h-3.5 cursor-pointer"
                aria-label="Include all models"
              />
            </label>
            <span className="w-2 h-2 rounded-full shrink-0" style={{ background: "#a78bfa" }} />
            <button
              type="button"
              onClick={() => { setScope({ kind: "all" }); setOpen(false); }}
              className="flex-1 pr-3 py-2 flex items-center gap-2 text-left"
            >
              <span className="truncate flex-1">All models</span>
              {scope.kind === "all" && <span className="text-[10px] text-fg-dim">●</span>}
            </button>
          </div>
          <div className="h-px bg-border" />
          {accounts.map((a, idx) => {
            const active = scope.kind === "model" && scope.accountId === a.id;
            const included = isIncluded(a.id);
            // Owner-group header before the first account of each owner.
            // Owner ordering matches the server-side sort (alphabetical
            // username) so the dividers land in stable positions.
            const ownerInfo = accountOwner?.get(a.id) ?? null;
            const prevOwnerInfo =
              idx > 0 ? accountOwner?.get(accounts[idx - 1].id) ?? null : null;
            const showOwnerHeader =
              groupByOwner && ownerInfo &&
              (idx === 0 || ownerInfo.id !== prevOwnerInfo?.id);
            return (
              <div key={a.id}>
                {showOwnerHeader && (
                  <div className="px-3 pt-2 pb-1 text-[10px] uppercase tracking-wide text-fg-dim border-t border-border first:border-t-0">
                    @{ownerInfo!.username}
                  </div>
                )}
              <div
                className={cn(
                  "w-full flex items-center gap-3 lg:gap-2 text-sm hover:bg-bg-elev-1",
                  active && "bg-bg-elev-1/60",
                )}
              >
                <label
                  className="pl-3 pr-1 lg:pr-0 py-3 lg:py-2 flex items-center cursor-pointer select-none"
                  title={
                    included
                      ? "Included in the all-models aggregate"
                      : "Excluded from the all-models aggregate"
                  }
                >
                  <input
                    type="checkbox"
                    checked={included}
                    onChange={() => toggle(a.id)}
                    className="w-5 h-5 lg:w-3.5 lg:h-3.5 cursor-pointer"
                  />
                </label>
                {canEditColor ? (
                  <>
                    {/* Below lg the swatch is read-only: a 16px <input type="color">
                        is a mis-tap that PATCHes the model colour for the whole
                        team. The editable input is restored verbatim at lg. */}
                    <span
                      className="lg:hidden w-2 h-2 rounded-full shrink-0"
                      style={{ background: a.color || "#666" }}
                    />
                    <input
                      type="color"
                      value={a.color || "#666666"}
                      onChange={(ev) => recolorM.mutate({ id: a.id, color: ev.target.value })}
                      onClick={(ev) => ev.stopPropagation()}
                      className="hidden lg:block w-4 h-4 rounded-full border border-border bg-transparent cursor-pointer p-0 shrink-0"
                      title="Set model color"
                      aria-label={`Color for ${a.nickname || a.id}`}
                    />
                  </>
                ) : (
                  <span
                    className="w-2 h-2 rounded-full shrink-0"
                    style={{ background: a.color || "#666" }}
                  />
                )}
                <button
                  type="button"
                  onClick={() => {
                    setScope({ kind: "model", accountId: a.id });
                    setOpen(false);
                  }}
                  className="flex-1 pr-3 py-2 flex items-center gap-2 text-left"
                >
                  <span className="truncate flex-1">{a.nickname || a.id}</span>
                  {!included && (
                    <span className="lg:hidden text-[10px] text-fg-dim shrink-0">excluded</span>
                  )}
                  {active && <span className="text-[10px] text-fg-dim">●</span>}
                </button>
              </div>
              </div>
            );
          })}
          {accounts.length === 0 && (
            <div className="p-3 text-xs text-fg-dim space-y-2">
              <div>No accounts yet.</div>
              <Link
                href="/setup"
                onClick={() => setOpen(false)}
                className="inline-block px-2 py-1 rounded border border-border hover:bg-bg-hover text-fg"
              >
                Go to Setup →
              </Link>
            </div>
          )}
        </div>
        </>
      )}
    </div>
  );
}
