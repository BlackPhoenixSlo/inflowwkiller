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

/** Optional branching map — reply_mass_funnel.resolve_next reads it to pick the
 *  step AFTER this one from the fan's reply, turning the linear walker into a
 *  branching storyline. Every value is a STEP NUMBER that must reference an
 *  existing step (funnels_api rejects a dangling target with 422). ABSENT `next`
 *  → the classic +1 advance. Precedence: keyword > bought > ignored > default. */
export interface StepNext {
  /** Jump here once the fan UNLOCKS a PPV (the #30 "offer taken" signal). */
  bought?: number;
  /** Jump here once the reply window is exhausted with no reply. */
  ignored?: number;
  /** {keyword → step}: a matching word in the reply routes past `default`. */
  keyword?: Record<string, number>;
  /** Fallback when no other branch matched (else the linear +1). */
  default?: number;
}

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
  /** Optional branching route from the fan's reply (see StepNext). */
  next?: StepNext;
}

/** A paid PPV step: send sales copy + locked media at a price.
 *
 *  NOTE: the MEDIA is no longer carried on the step — vault ids are per-account
 *  (an id from model A doesn't exist in model B's vault), so each model maps its
 *  own media via the per-account binding (useFunnelMedia / FunnelAccountMedia).
 *  `media_files`/`previews` remain in the type ONLY to read legacy funnels; the
 *  editor no longer writes them. */
export interface PpvStep {
  step: number;
  type: "paid_ppv";
  price?: number;
  /** @deprecated legacy in-step media — media is per-account now (see useFunnelMedia). */
  media_files?: Array<number | string>;
  /** @deprecated legacy in-step previews — see useFunnelMedia. */
  previews?: Array<number | string>;
  messages?: string[];
  prompt?: string;
  generate?: boolean;
  /** When true, the fan must pay to even read the copy. */
  locked_text?: boolean;
  /** Optional branching route (e.g. `{bought: n}` to an upsell — see StepNext). */
  next?: StepNext;
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

// ── Per-account media (funnel_account_media) ──────────────────────────
//
// A funnel's TEXT is global; its MEDIA is PER-ACCOUNT (OF vault ids don't carry
// between models). Each model maps its own opener + PPV-step vault media here,
// keyed by funnel step NUMBER. CRUD rides /admin/funnels/{id}/media/{account_id}
// (funnels_api.py) — owner-gated AND account-scoped (assert_account_owned).

const MEDIA_KEY = "funnel-media";

export interface FunnelStepMedia {
  media_files: number[];
  /** Subset of media_files shown FREE as the teaser (sent to OF as `previews`). */
  previews: number[];
}

export interface FunnelAccountMedia {
  funnel_id: number;
  account_id: string;
  /** The opener's vault media for this model. */
  opening_media_ids: number[];
  /** step number (string) → that step's per-account media. */
  steps_media: Record<string, FunnelStepMedia>;
}

export interface FunnelMediaSave {
  opening_media_ids?: number[];
  steps_media?: Record<string, FunnelStepMedia>;
}

/** One model's media binding for a funnel (empty defaults when none saved). */
export function useFunnelMedia(funnelId: number | null, accountId: string | null) {
  return useQuery<FunnelAccountMedia>({
    queryKey: [MEDIA_KEY, funnelId, accountId],
    enabled: funnelId != null && !!accountId,
    queryFn: () =>
      relay.get<FunnelAccountMedia>(
        `/admin/funnels/${funnelId}/media/${accountId}`,
        BG_CTX,
      ),
    staleTime: 30_000,
    refetchOnWindowFocus: false,
  });
}

export function useSaveFunnelMedia() {
  const qc = useQueryClient();
  return useMutation<
    FunnelAccountMedia,
    Error,
    { funnelId: number; accountId: string } & FunnelMediaSave
  >({
    mutationFn: ({ funnelId, accountId, ...body }) =>
      relay.put<FunnelAccountMedia>(
        `/admin/funnels/${funnelId}/media/${accountId}`,
        body,
      ),
    onSuccess: (_d, { funnelId, accountId }) =>
      qc.invalidateQueries({ queryKey: [MEDIA_KEY, funnelId, accountId] }),
  });
}
