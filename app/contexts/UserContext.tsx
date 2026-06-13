"use client";

/**
 * UserContext — "is anyone signed in" state for the friend-auth layer.
 *
 * Fetches /auth/me on mount. While `loading` is true the app shell
 * shows nothing (avoids a flash of the picker or app surface). When the
 * fetch returns:
 *   - user object → render children
 *   - null → redirect to /login (handled by the gate component)
 *
 * Login + logout are routed through here so consumers (TopNav, landing
 * form) don't have to know about the session cookie or refetch flow.
 *
 * Auth here is intentionally separate from the EmployeeContext picker
 * (which gates X-Employee-Id for audit). Order in providers.tsx:
 *   UserProvider → EmployeeProvider → ScopeProvider.
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";
import { useQueryClient } from "@tanstack/react-query";

// localStorage / sessionStorage keys to PRESERVE across identity switches.
// Anything else under the `chatterly:` or `chatterly-` prefix is wiped on
// every login / logout / register so a new identity boots with a fresh
// per-account state — vault picker MRU, scope selection, persisted React
// Query cache, notification history, etc.
//
// The previous wipe was a narrow two-key list; that left vault-state /
// scope / notification keys behind, which surfaced as a "not your account"
// 403 from the relay when the previous user's account_id sneaked into a
// fetch via stale cache. Aggressive identity-switch wiping closes that.
const IDENTITY_PRESERVE_KEYS = new Set<string>([
  "chatterly:theme",                            // UI preference
  "chatterly:share_token",                      // share-link gate
  "chatterly:chunk-reload-attempted",           // chunk-load retry guard
  "chatterly:chunk-reload-count",
  "chatterly:lockedText-default-changed-seen",  // one-time notice
  "chatterly:emoji_recents",                    // UI preference
  "chatterly:chat-sort-mode",                   // UI preference
  "chatterly:fan-drawer-default-open",          // UI preference
  "chatterly:compact-media",                    // UI preference
]);

export interface ImpersonatingDTO {
  real_user_id: string;
  real_username: string;
}

export interface AuthedUserDTO {
  user_id: string;
  username: string;
  /** Present when the founder is currently viewing this user via the
   *  /admin/impersonate overlay. The relay restricts the session to
   *  read-only requests while this is non-null. */
  impersonating?: ImpersonatingDTO | null;
}

interface UserContextValue {
  user: AuthedUserDTO | null;
  loading: boolean;
  /** Sign in with a known username/password. Throws RelayError on 4xx. */
  login: (username: string, password: string) => Promise<AuthedUserDTO>;
  /** Register a new friend. Throws on conflict / validation error. */
  register: (username: string, password: string) => Promise<AuthedUserDTO>;
  logout: () => Promise<void>;
  /** Founder-only: start viewing as another user. Wipes all caches so
   *  the next render reads fresh data for the target. */
  startImpersonating: (userId: string, adminPassword: string) => Promise<void>;
  /** Founder-only: clear the impersonate cookie + caches and re-fetch
   *  the founder's own /auth/me. */
  stopImpersonating: () => Promise<void>;
  /** Local-only identity flip: clears caches + sets user=null without
   *  hitting /auth/logout. Used by the chatter-login flow when
   *  transitioning principals on the same browser without two round-
   *  trips (logout, then chatter-login). */
  clearUserLocally: () => void;
}

const UserContext = createContext<UserContextValue | null>(null);

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

/** Wipe every chatterly storage key not on the preserve allowlist.
 *  Shared between UserContext + ChatterContext so both principals get the
 *  same aggressive wipe on identity switch. */
export function wipeIdentityStorage(): void {
  for (const storage of [
    typeof window !== "undefined" ? window.localStorage : null,
    typeof window !== "undefined" ? window.sessionStorage : null,
  ]) {
    if (!storage) continue;
    try {
      const victims: string[] = [];
      for (let i = 0; i < storage.length; i++) {
        const k = storage.key(i);
        if (!k) continue;
        if (!k.startsWith("chatterly:") && !k.startsWith("chatterly-")) continue;
        if (IDENTITY_PRESERVE_KEYS.has(k)) continue;
        victims.push(k);
      }
      for (const k of victims) storage.removeItem(k);
    } catch { /* safari private / quota — best effort */ }
  }
}

function wipeIdentityCaches(queryClient: ReturnType<typeof useQueryClient>) {
  // Cancel in-flight queries first so their `onSuccess` doesn't race the
  // clear and re-seed cache with previous-identity data after we've
  // wiped. cancelQueries returns a promise; we don't await — synchronous
  // clear() below is sufficient because RQ marks cancelled fetches as
  // such, and dehydration only persists successes.
  try { queryClient.cancelQueries(); } catch { /* ignore */ }
  // Drop in-memory React Query cache (everything, including non-persisted
  // keys — a stale chat list from the previous user is just as wrong as
  // a stale account list).
  try { queryClient.clear(); } catch { /* ignore */ }
  // Wipe identity-bound localStorage + sessionStorage. Anything under the
  // `chatterly:` / `chatterly-` namespace that isn't a preserved UI
  // preference dies here — vault picker state, scope selection,
  // notification history, persisted RQ cache, etc.
  wipeIdentityStorage();
  // Defeat the throttled-by-250ms persister: explicitly remove the RQ
  // snapshot AGAIN after a brief delay so any in-flight write triggered
  // by clear() doesn't re-stamp stale data we just removed. The
  // persister always serializes from current cache state, so by this
  // point either it wrote an empty snapshot (fine) or hasn't fired yet
  // (we win the race).
  try {
    setTimeout(() => {
      try { window.localStorage.removeItem("chatterly-rq-cache"); } catch { /* ignore */ }
    }, 350);
  } catch { /* ignore */ }
  // Fire-and-forget message to the image SW. It deletes the whole
  // chatterly-img-v3 Cache Storage bucket. SW is prod-only and may not
  // be registered (dev / insecure origin / first paint before register
  // resolves) — silently no-op in those cases.
  try {
    if (typeof navigator !== "undefined" && "serviceWorker" in navigator) {
      navigator.serviceWorker.getRegistration().then((reg) => {
        const sw = reg?.active;
        if (sw) sw.postMessage({ type: "clear-cache" });
      }).catch(() => { /* ignore */ });
    }
  } catch { /* ignore */ }
}

export function UserProvider({ children }: { children: React.ReactNode }) {
  const [user, setUser] = useState<AuthedUserDTO | null>(null);
  const [loading, setLoading] = useState(true);
  const queryClient = useQueryClient();

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const r = await fetch("/auth/me", {
          credentials: "same-origin",
          headers: { Accept: "application/json" },
        });
        if (cancelled) return;
        if (!r.ok) { setUser(null); return; }
        const body = (await r.json()) as AuthedUserDTO | null;
        setUser(body && body.user_id ? body : null);
      } catch {
        if (!cancelled) setUser(null);
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, []);

  // Re-validate /auth/me when the tab regains focus / becomes visible so a
  // stale impersonation overlay (e.g. another tab stopped impersonating, or
  // the cookie expired) self-corrects without a full reload. This never
  // touches `loading` (so it can't reflash the picker) and never re-fetches
  // on its own result, so there's no refetch loop — it only fires on real
  // user focus/visibility events. The initial load stays owned by the mount
  // effect above.
  useEffect(() => {
    let inFlight = false;
    const revalidate = async () => {
      if (inFlight) return;
      if (typeof document !== "undefined" && document.visibilityState === "hidden") return;
      inFlight = true;
      try {
        const r = await fetch("/auth/me", {
          credentials: "same-origin",
          headers: { Accept: "application/json" },
        });
        if (!r.ok) { setUser(null); return; }
        const body = (await r.json()) as AuthedUserDTO | null;
        setUser(body && body.user_id ? body : null);
      } catch {
        /* transient network hiccup — keep the current user, retry next focus */
      } finally {
        inFlight = false;
      }
    };
    const onVisibility = () => {
      if (typeof document !== "undefined" && document.visibilityState === "visible") {
        void revalidate();
      }
    };
    window.addEventListener("focus", revalidate);
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      window.removeEventListener("focus", revalidate);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, []);

  const login = useCallback(async (username: string, password: string) => {
    const dto = await postJSON<AuthedUserDTO>("/auth/login", { username, password });
    // Wipe any cache from the previous identity BEFORE flipping the user
    // state — otherwise the in-flight render reads the previous user's
    // data through the next paint.
    wipeIdentityCaches(queryClient);
    setUser(dto);
    return dto;
  }, [queryClient]);

  /** Raw cache wipe + setUser(null). Used by the chatter-login flow when
   *  it needs to flip identities without going through the network logout
   *  (the chatter is about to sign in via /chatter/login on the next call
   *  — we just want the local User state cleared so AuthGate doesn't keep
   *  rendering as the owner). */
  const clearUserLocally = useCallback(() => {
    wipeIdentityCaches(queryClient);
    setUser(null);
  }, [queryClient]);

  const register = useCallback(async (username: string, password: string) => {
    const dto = await postJSON<AuthedUserDTO>("/auth/register", { username, password });
    wipeIdentityCaches(queryClient);
    setUser(dto);
    return dto;
  }, [queryClient]);

  const logout = useCallback(async () => {
    try { await postJSON<unknown>("/auth/logout", {}); } catch { /* best effort */ }
    wipeIdentityCaches(queryClient);
    setUser(null);
    // Hard navigate so AuthGate evaluates from scratch. /auth/logout
    // now clears the chatter cookie defensively too, so a chatter ghost
    // could otherwise linger in cache (UserContext doesn't refetch
    // /auth/me after mount, ChatterContext's meQ races against the
    // cache wipe). A `.replace()` rather than `.assign()` so the back
    // button doesn't return to the signed-in URL.
    if (typeof window !== "undefined") {
      window.location.replace("/");
    }
  }, [queryClient]);

  const startImpersonating = useCallback(
    async (userId: string, adminPassword: string) => {
      // Set the overlay first; the relay validates admin_password and
      // throws on 403/404 before any cookie is set. Then wipe caches —
      // the next /auth/me + every downstream fetch resolves to the
      // impersonated user.
      const dto = await postJSON<{ impersonating: { user_id: string; username: string } }>(
        "/admin/impersonate",
        { user_id: userId, admin_password: adminPassword },
      );
      wipeIdentityCaches(queryClient);
      // Re-fetch /auth/me so `user.impersonating` flips on for the banner
      // without waiting for the next reload.
      const r = await fetch("/auth/me", {
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      });
      if (r.ok) {
        const body = (await r.json()) as AuthedUserDTO | null;
        setUser(body && body.user_id ? body : null);
      } else {
        // /auth/me hiccup is benign — UI will resync on next route change.
        // Fall back to a synthesized stub so the banner still renders.
        setUser({
          user_id: dto.impersonating.user_id,
          username: dto.impersonating.username,
        });
      }
    },
    [queryClient],
  );

  const stopImpersonating = useCallback(async () => {
    try {
      await fetch("/admin/impersonate", {
        method: "DELETE",
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      });
    } catch { /* best-effort */ }
    wipeIdentityCaches(queryClient);
    // Re-fetch /auth/me so the founder's real identity is back in `user`.
    try {
      const r = await fetch("/auth/me", {
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      });
      if (r.ok) {
        const body = (await r.json()) as AuthedUserDTO | null;
        setUser(body && body.user_id ? body : null);
      }
    } catch { /* swallow */ }
  }, [queryClient]);

  const value = useMemo<UserContextValue>(
    () => ({ user, loading, login, register, logout, startImpersonating, stopImpersonating, clearUserLocally }),
    [user, loading, login, register, logout, startImpersonating, stopImpersonating, clearUserLocally],
  );

  return <UserContext.Provider value={value}>{children}</UserContext.Provider>;
}

export function useUser(): UserContextValue {
  const ctx = useContext(UserContext);
  if (!ctx) throw new Error("useUser called outside <UserProvider>");
  return ctx;
}
