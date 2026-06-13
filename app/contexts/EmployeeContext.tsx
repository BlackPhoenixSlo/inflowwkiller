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
 * Phase D may layer real auth on top via a feature flag; the context API
 * here stays the same so views don't have to refactor.
 */

import { createContext, useContext, useEffect, useMemo, useState } from "react";
import type { Employee } from "@/lib/relay";

const LS_KEY = "chatterly:employee_id";

interface EmployeeContextValue {
  /** Currently-picked employee — null until the user chooses. */
  current: Employee | null;
  /** Full roster (active only by default). Populated by the picker modal. */
  roster: Employee[];
  setRoster: (rs: Employee[]) => void;
  /** Pick an employee id; throws if not in roster. Persisted to localStorage. */
  pick: (id: number) => void;
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
  const [roster, setRoster] = useState<Employee[]>([]);
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

  const current = useMemo(() => {
    if (pickedId == null) return null;
    return roster.find((e) => e.id === pickedId && e.is_active) ?? null;
  }, [pickedId, roster]);

  const pick = (id: number) => {
    const found = roster.find((e) => e.id === id);
    if (!found) {
      throw new Error(`employee ${id} not in roster`);
    }
    if (!found.is_active) {
      throw new Error(`employee ${found.display_name} is disabled`);
    }
    setPickedId(id);
    try { window.localStorage.setItem(LS_KEY, String(id)); } catch {}
  };

  const clear = () => {
    setPickedId(null);
    try { window.localStorage.removeItem(LS_KEY); } catch {}
  };

  return (
    <EmployeeContext.Provider value={{ current, roster, setRoster, pick, clear, hydrated, pickedId }}>
      {children}
    </EmployeeContext.Provider>
  );
}

export function useEmployee(): EmployeeContextValue {
  const ctx = useContext(EmployeeContext);
  if (!ctx) throw new Error("useEmployee must be used inside <EmployeeProvider>");
  return ctx;
}
