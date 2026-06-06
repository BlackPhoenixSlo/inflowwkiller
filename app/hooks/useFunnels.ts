"use client";

/**
 * useFunnels — data layer for the mass-message funnel editor (W8-P1).
 *
 * A funnel is a GLOBAL definition (no account scope) stored in
 * `mass_message_funnels`: one opener (`opening_message`, sent by A10
 * send_mass_message) plus an ordered list of `steps` that A11
 * reply_mass_funnel walks per replying fan. CRUD rides the relay's
 * /admin/funnels routes (service/funnels_api.py) — owner-gated, NOT
 * account-scoped, so the cache key is global.
 *
 * Mirrors app/hooks/useAutomations.ts (query + invalidating mutations).
 */

import {
  keepPreviousData,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";

import { relay, type RelayContext } from "@/lib/relay";

const KEY = "funnels";
const BG_CTX: RelayContext = { priority: "background" };

// ── Types (mirror funnels_api.py _validate_steps) ─────────────────────

/** A reply step: poll the fan for a reply on `check_intervals_min`, then send
 *  one of `messages` (first ≤2 used) — or generate one via `prompt`/`generate`. */
export interface ReplyStep {
  step: number;
  type?: undefined;
  /** Reply-poll cadence in minutes; absent → executor default [2,4,10]. */
  check_intervals_min?: number[];
  /** Static copy (variant pool). Empty → must set `prompt`/`generate`. */
  messages?: string[];
  prompt?: string;
  generate?: boolean;
}

/** A paid PPV step: send sales copy + locked media at a price. */
export interface PpvStep {
  step: number;
  type: "paid_ppv";
  price?: number;
  /** RAW vault ids (int / digit-string) — reply_mass_funnel does int(m). */
  media_files?: Array<number | string>;
  /** Subset of `media_files` shown FREE as the teaser (the leading N picked).
   *  reply_mass_funnel passes these to OF as `previews`. */
  previews?: Array<number | string>;
  messages?: string[];
  prompt?: string;
  generate?: boolean;
  /** When true, the fan must pay to even read the copy. */
  locked_text?: boolean;
}

export type FunnelStep = ReplyStep | PpvStep;

export interface FunnelSummary {
  id: number;
  name: string;
  description: string | null;
  opening_message: string;
  step_count: number;
}

export interface FunnelFull extends FunnelSummary {
  steps: FunnelStep[];
  /** Opener-media columns — surfaced read-only, NOT consumed at send today. */
  vault_folder: string | null;
  media_indices: unknown;
  opening_vault_folder: string | null;
  opening_media_indices: unknown;
}

// ── Request bodies ────────────────────────────────────────────────────

export interface FunnelCreate {
  name: string;
  opening_message: string;
  description?: string | null;
  steps?: FunnelStep[];
}

export interface FunnelUpdate {
  name?: string;
  opening_message?: string;
  description?: string | null;
  steps?: FunnelStep[];
}

// ── Reads ─────────────────────────────────────────────────────────────

export function useFunnels() {
  return useQuery<FunnelSummary[]>({
    queryKey: [KEY],
    queryFn: async () => {
      const r = await relay.get<{ funnels: FunnelSummary[] }>("/admin/funnels", BG_CTX);
      return r.funnels ?? [];
    },
    staleTime: 60_000,
    refetchOnWindowFocus: true,
    placeholderData: keepPreviousData,
  });
}

export function useFunnel(id: number | null) {
  return useQuery<FunnelFull>({
    queryKey: [KEY, id],
    enabled: id != null,
    queryFn: () => relay.get<FunnelFull>(`/admin/funnels/${id}`, BG_CTX),
    staleTime: 60_000,
    refetchOnWindowFocus: false,
  });
}

// ── Writes ────────────────────────────────────────────────────────────

export function useCreateFunnel() {
  const qc = useQueryClient();
  return useMutation<FunnelFull, Error, FunnelCreate>({
    mutationFn: (body) => relay.post<FunnelFull>("/admin/funnels", body),
    onSuccess: () => qc.invalidateQueries({ queryKey: [KEY] }),
  });
}

export function useUpdateFunnel() {
  const qc = useQueryClient();
  return useMutation<FunnelFull, Error, { id: number } & FunnelUpdate>({
    mutationFn: ({ id, ...patch }) => relay.put<FunnelFull>(`/admin/funnels/${id}`, patch),
    onSuccess: (_d, { id }) => {
      qc.invalidateQueries({ queryKey: [KEY] });
      qc.invalidateQueries({ queryKey: [KEY, id] });
    },
  });
}

export function useDeleteFunnel() {
  const qc = useQueryClient();
  return useMutation<unknown, Error, number>({
    mutationFn: (id) => relay.delete(`/admin/funnels/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: [KEY] }),
  });
}
