"use client";

/**
 * ChatterContext — second principal alongside UserContext.
 *
 * A chatter signs in once at /chatter-login and sees the SET UNION of
 * every linked owner's UserAccount rows in the picker. Switching between
 * accounts in the picker swaps the per-account view; WebSocket
 * subscriptions for previously-opened accounts stay alive so coming back
 * is instant (see Todo.txt STEP 8 constraint 5).
 *
 * Two React Query keys hydrate this context:
 *   ["chatter", "self"]            → /chatter/me      (chatter + owners)
 *   ["chatter", "self", "accounts"] → /chatter/me/accounts (flat list)
 *
 * Both run with `placeholderData: keepPreviousData` so the picker
 * dropdown does NOT block during a background refetch — a chatter
 * never sees the picker collapse while we re-fetch their owners.
 *
 * The active principal (User vs Chatter) is decided ONCE at the
 * provider layer: if a User cookie is set (UserContext.user is truthy),
 * the owner perspective wins everywhere. The Chatter context still
 * fires its queries so the chatter-login flow can complete, but the
 * picker / shell reads from UserContext until logout.
 */

import {
  createContext,
  useCallback,
  useContext,
  useMemo,
} from "react";
import {
  keepPreviousData,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";

import { wipeIdentityStorage } from "@/contexts/UserContext";

export interface ChatterOwnerAccountDTO {
  account_id: string;
  nickname: string | null;
  color: string | null;
}

export interface ChatterOwnerDTO {
  user_id: string;
  username: string;
  accounts: ChatterOwnerAccountDTO[];
}

export interface ChatterMeDTO {
  chatter_id: string;
  username: string;
  display_name: string | null;
  color: string | null;
  owners: ChatterOwnerDTO[];
}

export interface ChatterFlatAccountDTO {
  account_id: string;
  nickname: string | null;
  color: string | null;
  owner_id: string;
  owner_username: string;
}

interface ChatterContextValue {
  chatter: ChatterMeDTO | null;
  /** Flat list with owner_* denormalised — what the picker should render. */
  accounts: ChatterFlatAccountDTO[];
  /** Same data grouped by owner for the picker's owner headers. */
  groupedAccounts: ChatterOwnerDTO[];
  /** True until the first /chatter/me fetch resolves — gate render with this. */
  loading: boolean;
  /** Sign in with a username/password. Throws Error on 4xx. */
  login: (username: string, password: string) => Promise<ChatterMeDTO>;
  /** Sign up + auto-link via an owner-issued invite token. */
  register: (
    username: string,
    password: string,
    token: string,
    displayName?: string,
  ) => Promise<ChatterMeDTO>;
  logout: () => Promise<void>;
}

const ChatterContext = createContext<ChatterContextValue | null>(null);

async function postJSON<T>(path: string, body: unknown): Promise<T> {
  const r = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify(body),
    credentials: "same-origin",
  });
  const text = await r.text();
  let parsed: unknown = text;
  try { parsed = text ? JSON.parse(text) : null; } catch { /* keep text */ }
  if (!r.ok) {
    const detail =
      (typeof parsed === "object" && parsed && "detail" in parsed)
        ? String((parsed as Record<string, unknown>).detail)
        : `HTTP ${r.status}`;
    throw new Error(detail);
  }
  return parsed as T;
}

function wipeNonChatterCaches(queryClient: ReturnType<typeof useQueryClient>) {
  // Cancel in-flight queries from the previous identity so their resolved
  // payloads don't race the clear and re-seed the cache. Chatter family
  // is freshly seeded by login() right before this call — cancelQueries
  // doesn't touch synchronous setQueryData, so the seed survives.
  try {
    queryClient.cancelQueries({
      predicate: (q) => {
        const head = q.queryKey[0];
        return !(typeof head === "string" && head === "chatter");
      },
    });
  } catch { /* ignore */ }
  // Drop every previous-identity query EXCEPT the `chatter` family. The
  // order matters: ChatterContext.login() seeds ["chatter", "self"]
  // BEFORE calling this, and any predicate-based removal that included
  // the chatter family would briefly null it back out, causing the
  // AuthGate to flash Landing for one render between the seed and the
  // next setQueryData notification. (Symptom: "click login does nothing,
  // refresh works.") Removing only non-chatter queries keeps the
  // freshly-seeded chatter state stable across the navigation.
  try {
    queryClient.removeQueries({
      predicate: (q) => {
        const head = q.queryKey[0];
        return !(typeof head === "string" && head === "chatter");
      },
    });
  } catch { /* ignore */ }
  // Wipe identity-bound localStorage + sessionStorage. Shared helper with
  // UserContext so both principals get the same aggressive sweep.
  wipeIdentityStorage();
  // Defeat the throttled persister: re-remove the RQ snapshot after the
  // 250ms throttle window so any pending write (triggered by the
  // removeQueries above) doesn't re-stamp stale data we just wiped.
  try {
    setTimeout(() => {
      try { window.localStorage.removeItem("chatterly-rq-cache"); } catch { /* ignore */ }
    }, 350);
  } catch { /* ignore */ }
}

export function ChatterProvider({ children }: { children: React.ReactNode }) {
  const queryClient = useQueryClient();

  const meQ = useQuery<ChatterMeDTO | null>({
    queryKey: ["chatter", "self"],
    queryFn: async () => {
      const r = await fetch("/chatter/me", {
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      });
      if (!r.ok) return null;
      const body = (await r.json()) as ChatterMeDTO | null;
      return body && body.chatter_id ? body : null;
    },
    staleTime: 5 * 60_000,
    placeholderData: keepPreviousData,
    // Identity transitions should refetch eagerly so the AuthGate
    // resolves the new principal without a hard reload.
    refetchOnMount: true,
  });

  const accountsQ = useQuery<ChatterFlatAccountDTO[]>({
    queryKey: ["chatter", "self", "accounts"],
    queryFn: () =>
      fetch("/chatter/me/accounts", {
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      }).then(async (r) => {
        if (!r.ok) return [];
        return (await r.json()) as ChatterFlatAccountDTO[];
      }),
    staleTime: 5 * 60_000,
    placeholderData: keepPreviousData,
    // Disable until we know the chatter is signed in — saves a wasted
    // 401 fetch on every owner-side page load.
    enabled: !!meQ.data,
  });

  const accounts = accountsQ.data ?? [];
  const chatter = meQ.data ?? null;
  const groupedAccounts = chatter?.owners ?? [];

  const login = useCallback(async (username: string, password: string) => {
    const dto = await postJSON<ChatterMeDTO>("/chatter/login", { username, password });
    // Seed FIRST so the chatter principal is visible the instant any
    // observer re-renders, then wipe the rest of the cache. Reversed
    // order caused the "have to refresh" bug.
    queryClient.setQueryData(["chatter", "self"], dto);
    wipeNonChatterCaches(queryClient);
    return dto;
  }, [queryClient]);

  const register = useCallback(
    async (username: string, password: string, token: string, displayName?: string) => {
      const url = `/chatter/register?token=${encodeURIComponent(token)}`;
      const dto = await postJSON<ChatterMeDTO>(url, {
        username, password, display_name: displayName,
      });
      queryClient.setQueryData(["chatter", "self"], dto);
      wipeNonChatterCaches(queryClient);
      return dto;
    },
    [queryClient],
  );

  const logout = useCallback(async () => {
    try { await postJSON<unknown>("/chatter/logout", {}); } catch { /* best effort */ }
    // Full wipe — including the chatter family — because logout is the
    // "start over" moment. cancelQueries first so resolving fetches
    // don't repopulate the cache we're about to drop.
    try { queryClient.cancelQueries(); } catch { /* ignore */ }
    try { queryClient.clear(); } catch { /* ignore */ }
    queryClient.setQueryData(["chatter", "self"], null);
    queryClient.setQueryData(["chatter", "self", "accounts"], []);
    wipeIdentityStorage();
    try { window.localStorage.removeItem("chatterly-rq-cache"); } catch { /* ignore */ }
    // Hard navigate so AuthGate evaluates with no cookies and no
    // lingering React state. The previous setState-only path lost a
    // race with the meQ observer's implicit refetch (which surfaced the
    // previous chatter via `placeholderData: keepPreviousData` for the
    // next render), forcing users to manually reload after clicking
    // logout. A `.replace()` rather than `.assign()` so the back button
    // doesn't return them to the signed-in URL.
    if (typeof window !== "undefined") {
      window.location.replace("/");
    }
  }, [queryClient]);

  const value = useMemo<ChatterContextValue>(() => ({
    chatter,
    accounts,
    groupedAccounts,
    loading: meQ.isLoading,
    login,
    register,
    logout,
  }), [chatter, accounts, groupedAccounts, meQ.isLoading, login, register, logout]);

  return <ChatterContext.Provider value={value}>{children}</ChatterContext.Provider>;
}

export function useChatter(): ChatterContextValue {
  const ctx = useContext(ChatterContext);
  if (!ctx) throw new Error("useChatter called outside <ChatterProvider>");
  return ctx;
}
