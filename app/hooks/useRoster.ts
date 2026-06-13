"use client";

/**
 * useRoster — the owner's employee roster, shared across the settings tabs
 * (Employees / Transfer / Audit).
 *
 * Backend: GET /admin/employees. The signed-in user only ever sees their
 * own roster (employees.py filters by user_id). `include_disabled=true`
 * (the default) also returns soft-disabled rows so tables can render them
 * strikethrough; pass `includeDisabled: false` to drop them.
 *
 * All three tabs previously inlined this same query under the SAME cache
 * key (`["employees", "all"]`) but with INCONSISTENT staleTime — whichever
 * tab mounted first won. Converging on ONE hook (and one staleTime) fixes
 * that; mutations elsewhere still invalidate `["employees"]`.
 *
 * `rows` is the chatter-mirror-filtered view (auto-created Employee rows
 * with a `chatter_id` belong to the Chatters tab, not the owner roster).
 * Pass `includeChatterMirrors: true` to keep them — the audit log needs the
 * full set so rows attributed to a chatter mirror still resolve a name.
 */

import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";

import { relay, type Employee } from "@/lib/relay";

export interface EmployeesResp { employees: Employee[]; }

export interface UseRosterOptions {
  /** Include soft-disabled rows (default true). */
  includeDisabled?: boolean;
  /** Keep auto-created chatter-mirror rows in `rows` (default false). */
  includeChatterMirrors?: boolean;
}

export function useRoster(opts: UseRosterOptions = {}) {
  const { includeDisabled = true, includeChatterMirrors = false } = opts;

  const query = useQuery<EmployeesResp>({
    queryKey: ["employees", "all"],
    queryFn: () =>
      relay.get<EmployeesResp>(
        `/admin/employees?include_disabled=${includeDisabled ? "true" : "false"}`,
      ),
    staleTime: 10_000,
  });

  const rows = useMemo(() => {
    const all = query.data?.employees ?? [];
    return includeChatterMirrors ? all : all.filter((e) => !e.chatter_id);
  }, [query.data?.employees, includeChatterMirrors]);

  return { query, rows };
}
