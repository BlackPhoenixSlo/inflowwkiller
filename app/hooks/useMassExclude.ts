"use client";

/**
 * useMassExclude — read + toggle a fan's membership on one of the account's two
 * system mass-exclude lists:
 *   kind="ppv" → MASSppvEXCLUDE  (skipped from mass PPV sends — priced broadcasts)
 *   kind="dm"  → MASSdmEXCLUDE   (skipped from mass DM sends + online blast)
 * Backed by a local kind='exclude' list mirrored to a same-named pinned OF list
 * (service/lists.py + audiences.exclude_list_fan_ids). Optimistic toggle;
 * account_id/fan_id/kind go in the query string (the relay's /admin/* endpoints
 * read them as params — the X-Account-Id header is for audit/scope only).
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { relay } from "@/lib/relay";

export type MassExcludeKind = "ppv" | "dm";

interface MassExcludeResp {
  /** Backend endpoint is /admin/do-not-mass (kind param); response key is
   *  do_not_mass for backward-compat with the single-list callers. */
  do_not_mass: boolean;
  /** False when the OnlyFans-list mirror couldn't be synced (no session / OF
   *  down). The local exclusion still applies; the UI flags a pending sync. */
  of_synced?: boolean;
  of_list_id?: number | null;
}

function url(accountId: string, fanId: number, kind: MassExcludeKind) {
  return `/admin/do-not-mass?account_id=${encodeURIComponent(accountId)}&fan_id=${fanId}&kind=${kind}`;
}

export function useMassExclude(accountId: string, fanId: number, kind: MassExcludeKind) {
  const qc = useQueryClient();
  const key = ["mass-exclude", accountId, fanId, kind] as const;

  const q = useQuery({
    queryKey: key,
    enabled: !!accountId && !!fanId,
    staleTime: 60_000,
    queryFn: () => relay.get<MassExcludeResp>(url(accountId, fanId, kind), { accountId }),
  });

  const toggle = useMutation({
    mutationFn: (next: boolean): Promise<MassExcludeResp> =>
      next
        ? relay.post<MassExcludeResp>(url(accountId, fanId, kind), undefined, { accountId })
        : relay.delete<MassExcludeResp>(url(accountId, fanId, kind), { accountId }),
    onMutate: async (next: boolean) => {
      await qc.cancelQueries({ queryKey: key });
      const prev = qc.getQueryData<MassExcludeResp>(key);
      qc.setQueryData<MassExcludeResp>(key, { do_not_mass: next });
      return { prev };
    },
    onError: (_e, _next, ctx) => {
      if (ctx?.prev) qc.setQueryData(key, ctx.prev);
    },
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: key });
    },
  });

  return { isOn: !!q.data?.do_not_mass, isLoading: q.isLoading, toggle };
}
