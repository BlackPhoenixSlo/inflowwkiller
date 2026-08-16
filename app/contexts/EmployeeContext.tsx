"use client";

/**
 * EmployeeContext — "who am I" state for the auth-lite picker.
 *
 * On first load, the picker modal renders full-screen until the user
 * chooses an employee. The choice is persisted to localStorage and used
 * to populate `X-Employee-Id` on every relay call (via relay.ts ctx).
 *
 * Re-pick from the top bar at any time. Clearing localStorage forces the
 * modal back. No passwords, no sessions — your team won't scam each other
 * (per the plan §5).
 *
 * `roster` is derived from useRoster() during render. It used to be
 * provider state that EmployeePickerGate copied in via useEffect while
 * rendering its buttons off a query of its own — two sources of truth one
 * commit apart, so the commit that PAINTED the picker wired every button
 * to a `pick` closed over an empty roster. A click landing there threw
 * "employee N not in roster" past the ErrorBoundary (React re-throws
 * event-handler errors to window) and silently did nothing. One source,
 * read during render, means no such commit exists.
 *
 * Phase D may layer real auth on top via a feature flag; the context API
 * here stays the same so views don't have to refactor.
 */

import { createContext, useContext, useEffect, useMemo, useState } from "react";

import { useRoster } from "@/hooks/useRoster";
import type { Employee } from "@/lib/relay";

const LS_KEY = "chatterly:employee_id";

/** Stable identity for the "no roster yet" case so `current`'s memo and
 *  every consumer's deps don't churn on each render. */
const EMPTY_ROSTER: Employee[] = [];

interface EmployeeContextValue {
  /** Currently-picked employee — null until the user chooses. */
  current: Employee | null;
  /** Full roster, disabled rows and chatter mirrors included — attribution
   *  flows (orphan tips, audit) need to resolve a name for every id. The
   *  picker narrows it to pickable rows itself. */
  roster: Employee[];
  /** Record who's at the keyboard. Takes the row rather than an id: every
   *  caller picks out of the roster it just rendered, so there is no id to
   *  validate and no failure mode to handle. */
  pick: (employee: Employee) => void;
  /** Clear the pick. Browser will re-show the picker on next render. */
  clear: () => void;
  /** True once we've completed first-load hydration. Use this to gate
   *  rendering of authenticated screens — null during SSR / first paint. */
  hydrated: boolean;
  /** localStorage-restored picked id — set even before `roster` lands,
   *  so the gate can tell "user has a pick, just waiting for roster"
   *  apart from "fresh install, needs picker". */
  pickedId: number | null;
}

const EmployeeContext = createContext<EmployeeContextValue | null>(null);

export function EmployeeProvider({ children }: { children: React.ReactNode }) {
  const [pickedId, setPickedId] = useState<number | null>(null);
  const [hydrated, setHydrated] = useState(false);

  // Hydrate from localStorage on mount. Effect, not lazy state, because
  // Next 16 server-renders this provider and localStorage is browser-only.
  useEffect(() => {
    try {
      const raw = window.localStorage.getItem(LS_KEY);
      const n = raw ? parseInt(raw, 10) : null;
      if (n !== null && Number.isFinite(n)) setPickedId(n);
    } catch {
      /* private mode / safari quota — fall back to fresh picker */
    } finally {
      setHydrated(true);
    }
  }, []);

  // Same cache entry the settings tabs and the picker read — the gate calls
  // useRoster() too and therefore renders this exact array.
  const { query } = useRoster();
  const roster = query.data?.employees ?? EMPTY_ROSTER;

  const current = useMemo(() => {
    if (pickedId == null) return null;
    return roster.find((e) => e.id === pickedId && e.is_active) ?? null;
  }, [pickedId, roster]);

  const pick = (employee: Employee) => {
    setPickedId(employee.id);
    try { window.localStorage.setItem(LS_KEY, String(employee.id)); } catch {}
  };

  const clear = () => {
    setPickedId(null);
    try { window.localStorage.removeItem(LS_KEY); } catch {}
  };

  return (
    <EmployeeContext.Provider value={{ current, roster, pick, clear, hydrated, pickedId }}>
      {children}
    </EmployeeContext.Provider>
  );
}

export function useEmployee(): EmployeeContextValue {
  const ctx = useContext(EmployeeContext);
  if (!ctx) throw new Error("useEmployee must be used inside <EmployeeProvider>");
  return ctx;
}
