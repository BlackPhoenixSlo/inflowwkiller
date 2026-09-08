"use client";

/**
 * Which model the /vault page is looking at — one answer, shared by everything
 * on it.
 *
 * There used to be three. `VaultManagePanel`, `VaultReviewTab` and
 * `VaultImportCard` each held their own `accountId` and each defaulted it to
 * `accounts[0]`, which agrees right up until somebody picks a different model:
 * the panel's chips moved its own state and nothing else's, so the import card
 * went on uploading into the FIRST account's vault — and offering that
 * account's folder ids, which are submitted to OnlyFans verbatim — while the
 * grid above it displayed a different model entirely. Files landing in the
 * wrong creator's vault is not a UI inconsistency.
 *
 * The choice is remembered per browser, so a reload comes back to the model you
 * were working on rather than to whichever account happens to sort first.
 * `useActiveAccounts` is the authority on what can be PICKED: a remembered id
 * whose session has since dropped is not selectable. It is deliberately NOT the
 * authority on what exists — see `sessionLost` below.
 */

import {
  createContext, useCallback, useContext, useEffect, useMemo, useState,
} from "react";

import { useAccounts, useActiveAccounts } from "@/hooks/useAccounts";
import type { AccountMeta } from "@/lib/relay";

const STORE_KEY = "vault.accountId";

type VaultAccountCtx = {
  /** null until the account list has loaded and a model has been adopted. */
  accountId: string | null;
  /** The selected account's row, for surfaces that want to NAME the model.
   *  Read from the FULL registry, not the session-backed subset, so a model
   *  whose session just dropped is still named rather than reverting to "…". */
  account: AccountMeta | null;
  /** The models that can be picked — the session-backed ones. */
  accounts: AccountMeta[];
  /** The selected model still exists but its session has gone. The page keeps
   *  pointing at it (see the adoption effect) and says so. */
  sessionLost: boolean;
  /** The account list has answered at least once. Distinguishes "no models with
   *  a session" from "we do not know yet", which look identical otherwise. */
  ready: boolean;
  setAccountId: (id: string) => void;
};

const Ctx = createContext<VaultAccountCtx | null>(null);

/** Read the remembered choice. Storage can throw (Safari private mode, a
 *  locked-down profile), and a picker that crashes the page is worse than one
 *  that forgets. */
function remembered(): string | null {
  try {
    return window.localStorage.getItem(STORE_KEY);
  } catch {
    return null;
  }
}

export function VaultAccountProvider({ children }: { children: React.ReactNode }) {
  const accounts = useActiveAccounts();
  // The FULL registry, session or not. Needed to tell the two ways a selected
  // model can leave `accounts` apart — they want opposite responses.
  const all = useAccounts().data?.accounts;
  const [accountId, setId] = useState<string | null>(null);

  const known = accountId != null && (all ?? []).some((a) => a.id === accountId);
  const pickable = accountId != null && accounts.some((a) => a.id === accountId);
  const sessionLost = known && !pickable;

  // Adopt a selection, and re-adopt ONLY when the selected model has genuinely
  // stopped existing.
  //
  // The distinction is the whole of this effect. `accounts` is filtered on
  // `has_session`, and a session dropping is a first-class event here —
  // `session_dead_at` and `session_dead_reason` are columns. Re-adopting
  // `accounts[0]` on that event moves the operator to a DIFFERENT CREATOR in
  // silence, and because the page keys its children on this id, it does so by
  // remounting them mid-action: the grid selection, the open drawer, the folder
  // filter, the search box, the reorder buffer and the files staged in the
  // import picker all go, with no message. It never comes back either — the
  // auto-adopted id is not written to storage, and once it is a member this
  // effect early-returns.
  //
  // So a dead session KEEPS the selection (`sessionLost` renders the reason,
  // and every query on the page is already scoped to an account the relay will
  // refuse). Only an id that is in no account row at all — a model actually
  // removed from the registry — is dropped, and even then the remembered id
  // stays in storage as the recovery target for when it comes back.
  useEffect(() => {
    if (all === undefined) return;          // the list has not answered yet
    if (accountId !== null && known) return;
    if (accounts.length === 0) {
      // Nothing to adopt. Drop a selection that is no longer in the registry
      // anyway, so the page falls back to its "no models" state instead of
      // mounting the whole subtree under a model that does not exist and
      // watching every request 404. A selection that IS still in the registry
      // is kept — that is `sessionLost`, and it is a wait, not a removal.
      if (accountId !== null && !known) setId(null);
      return;
    }
    const saved = remembered();
    setId(accounts.find((a) => a.id === saved)?.id ?? accounts[0].id);
  }, [accounts, all, accountId, known]);

  const setAccountId = useCallback((id: string) => {
    setId(id);
    try {
      window.localStorage.setItem(STORE_KEY, id);
    } catch {
      /* remembering is a convenience; not remembering is not an error */
    }
  }, []);

  const value = useMemo<VaultAccountCtx>(
    () => ({
      accountId,
      account: (all ?? []).find((a) => a.id === accountId) ?? null,
      accounts,
      sessionLost,
      ready: all !== undefined,
      setAccountId,
    }),
    [accountId, accounts, all, sessionLost, setAccountId],
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useVaultAccount(): VaultAccountCtx {
  const v = useContext(Ctx);
  if (!v) {
    throw new Error("useVaultAccount must be used inside <VaultAccountProvider>");
  }
  return v;
}

/** The model this subtree is scoped to, as a plain string.
 *
 *  Non-null BY CONSTRUCTION: `VaultBody` does not mount the grid, the importer
 *  or the review queue until a model has been adopted, and remounts them when
 *  it changes. That is what lets everything below stop carrying a null branch
 *  for a state it can never observe — nineteen `!accountId` guards, a "…"
 *  placeholder name, a disabled Start button and two copies of the same empty
 *  state, all for one render that no longer happens. Throwing is right: a
 *  caller reaching here with no model has escaped that mount rule, and a
 *  request scoped to whatever the relay guesses is the bug this page exists to
 *  prevent. */
export function useVaultAccountId(): string {
  const { accountId } = useVaultAccount();
  if (accountId === null) {
    throw new Error(
      "useVaultAccountId requires a selected model — mount this inside "
      + "VaultBody's accountId branch, not above it",
    );
  }
  return accountId;
}
