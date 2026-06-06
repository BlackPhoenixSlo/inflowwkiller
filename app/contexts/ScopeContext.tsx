"use client";

/**
 * ScopeContext — current inbox scope: "all models" or a single model.
 *
 * Persists in localStorage so refreshing keeps you in the same view.
 * Drives:
 *   • Which `X-Account-Id` header gets sent on relay calls
 *   • Which SSE scope the EventSource subscribes to (eventBus.setScope)
 *   • Which IndexedDB queries filter (db.chats.where('accountId').equals(...))
 *
 * Phase B's Unified Inbox is built on top of this — switching scope
 * doesn't reload data, it just changes the filter the views apply.
 */

import { createContext, useContext, useEffect, useMemo, useState } from "react";
import { eventBus, type Scope as EventScope } from "@/lib/events";

const LS_KEY = "chatterly:scope";

export type Scope =
  | { kind: "all" }
  | { kind: "model"; accountId: string };

interface ScopeContextValue {
  scope: Scope;
  setScope: (s: Scope) => void;
  /** Convenience: the X-Account-Id header to send (null for "all"). */
  accountId: string | null;
  /** Convenience: the SSE scope string the relay expects. */
  eventScope: EventScope;
}

const ScopeContext = createContext<ScopeContextValue | null>(null);

function serialize(s: Scope): string {
  return s.kind === "all" ? "all" : `model:${s.accountId}`;
}
function parse(raw: string | null): Scope {
  if (!raw || raw === "all") return { kind: "all" };
  if (raw.startsWith("model:")) {
    const accountId = raw.slice("model:".length);
    if (accountId) return { kind: "model", accountId };
  }
  return { kind: "all" };
}

export function ScopeProvider({ children }: { children: React.ReactNode }) {
  const [scope, setScopeState] = useState<Scope>({ kind: "all" });

  // Hydrate from localStorage. Same pattern as EmployeeContext —
  // browser-only so we do it in an effect, not lazy state.
  useEffect(() => {
    try {
      const raw = window.localStorage.getItem(LS_KEY);
      setScopeState(parse(raw));
    } catch { /* ignore */ }
  }, []);

  // Push every scope change down to the SSE bus so events get filtered
  // server-side. This is fire-and-forget; the bus tears down + reopens.
  useEffect(() => {
    eventBus.setScope(serialize(scope) as EventScope);
  }, [scope]);

  const setScope = (s: Scope) => {
    setScopeState(s);
    try { window.localStorage.setItem(LS_KEY, serialize(s)); } catch {}
  };

  const value = useMemo<ScopeContextValue>(() => ({
    scope,
    setScope,
    accountId: scope.kind === "model" ? scope.accountId : null,
    eventScope: serialize(scope) as EventScope,
  }), [scope]);

  return <ScopeContext.Provider value={value}>{children}</ScopeContext.Provider>;
}

export function useScope(): ScopeContextValue {
  const ctx = useContext(ScopeContext);
  if (!ctx) throw new Error("useScope must be used inside <ScopeProvider>");
  return ctx;
}
