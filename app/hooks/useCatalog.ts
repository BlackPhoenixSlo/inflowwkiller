import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { relay, type RelayContext } from "@/lib/relay";

const KEY = "ai-chatter";
const BG_CTX: RelayContext = { priority: "background" };

/** One sellable unit. `description_for_ai` is the pitch contract — what the
 *  fan actually SEES, present tense; the LLM may only claim what's written. */
export interface CatalogItemT {
  id?: number;
  script_id?: number | null;
  position?: number | null;
  kind: "video" | "image" | "image_set";
  label?: string | null;
  description_for_ai?: string | null;
  media_ids: number[];
  preview_media_ids: number[];
  duration_sec?: number | null;
  price_cents: number;
  tip_unlock_cents: number;
  is_free_teaser: boolean;
  tags: string[];
  enabled: boolean;
  stats?: { offers: number; delivered: number };
}

export interface CatalogScriptT {
  id: number;
  name: string;
  theme?: string | null;
  status: "draft" | "enabled" | "disabled";
  items: CatalogItemT[];
}

export interface ScriptsResponse {
  account_id: string;
  scripts: CatalogScriptT[];
  singles: CatalogItemT[];
}

export interface AiChatterConfig {
  enabled?: boolean;
  mode?: "backup" | "always";
  /** Closer mode: only reply to a fan who shows buying intent (or has an open
   *  offer); leave pure chit-chat to the team / Auto Convo. */
  intent_only?: boolean;
  sla_minutes?: number;
  max_lifetime_spend_cents?: number;
  offer_mode?: "tip" | "ppv" | "both";
  max_offers_per_fan_per_day?: number;
  min_fan_msgs_between_offers?: number;
  /** Closer pivots tease→offer when the fan leans in / gets physical
   *  (ESCALATION_RE), not only on an explicit "show me". Bound by the pacing caps. */
  pivot_on_escalation?: boolean;
  /** "Chat a bit first": no lean-in pivot until the fan has sent >= this many msgs. */
  min_fan_msgs_before_escalation_pitch?: number;
  /** On offer expiry, unsend (pull) the unpurchased PPV/offer message from the chat. */
  unsend_expired_offer?: boolean;
  max_fans_per_tick?: number;
  resume_after_manual_hours?: number;
  stall_ttl_hours?: number;

  /** Cadence controller (items 10/17/18/21) — makes the bot back off deliberately
   *  instead of chatting/selling forever. OFF by default (historical behavior). */
  cadence_enabled?: boolean;
  /** Per-signal reply caps within a burst (0 = unlimited). Sent as a COMPLETE
   *  object — the server fills any missing tier from defaults. */
  msg_limits_by_signal?: {
    baseline?: number;
    buying_signal?: number;
    no_signal?: number;
    pic_sent?: number;
  };
  /** Silence gap (min) that starts a fresh burst for the caps above. */
  session_gap_minutes?: number;
  /** Keep chatting a just-paid fan this long (min); past it with no new spend, hand off. */
  post_purchase_minutes?: number;
  /** A pending unbought offer older than this (min) drops to the short-leash tier. */
  offer_expiry_minutes?: number;
  /** One gentle re-engage nudge if an offered fan goes quiet without buying.
   *  The only cadence piece that SENDS — OFF by default even when cadence is on. */
  nudge_enabled?: boolean;
  nudge_after_minutes?: number;
}

interface AiChatterConfigResponse {
  account_id: string;
  config: AiChatterConfig;
  defaults: AiChatterConfig;
}

export interface SessionRow {
  fan_id: number;
  fan_name: string;
  script: string;
  position: number;
  status: string;
  updated_at: string | null;
}

export interface OfferRow {
  id: number;
  fan_id: number;
  fan_name: string;
  item_label: string | null;
  mode: string;
  status: string;
  price_cents: number;
  tip_unlock_cents: number;
  tips_accum_cents: number;
  resolved_by: string | null;
  offered_at: string | null;
}

export interface SimulateResponse {
  bubbles: string[];
  offer: {
    item_id: number;
    label: string | null;
    price_cents: number;
    tip_unlock_cents: number;
    is_free_teaser: boolean;
  } | null;
  offerable_count: number;
  manifest_present: boolean;
}

const aid = (accountId: string | null) => encodeURIComponent(accountId ?? "");

export function useAiChatterConfig(accountId: string | null) {
  return useQuery<AiChatterConfigResponse>({
    queryKey: [KEY, "config", accountId],
    enabled: !!accountId,
    queryFn: () =>
      relay.get(`/admin/ai-chatter-config?account_id=${aid(accountId)}`, BG_CTX),
    staleTime: 60_000,
    refetchOnWindowFocus: false,
  });
}

export function useSaveAiChatterConfig(accountId: string | null) {
  const qc = useQueryClient();
  return useMutation<unknown, Error, AiChatterConfig>({
    mutationFn: (config) =>
      relay.put(`/admin/ai-chatter-config`, { account_id: accountId, config }),
    onSuccess: () => qc.invalidateQueries({ queryKey: [KEY, "config", accountId] }),
  });
}

export function useCatalogScripts(accountId: string | null) {
  return useQuery<ScriptsResponse>({
    queryKey: [KEY, "scripts", accountId],
    enabled: !!accountId,
    queryFn: () => relay.get(`/admin/scripts?account_id=${aid(accountId)}`, BG_CTX),
    staleTime: 30_000,
    refetchOnWindowFocus: false,
  });
}

export function useUpsertScript(accountId: string | null) {
  const qc = useQueryClient();
  return useMutation<{ id: number }, Error,
    { id?: number; name: string; theme?: string; status?: string }>({
    mutationFn: (body) =>
      relay.post(`/admin/scripts`, { account_id: accountId, ...body }),
    onSuccess: () => qc.invalidateQueries({ queryKey: [KEY, "scripts", accountId] }),
  });
}

export function useDeleteScript(accountId: string | null) {
  const qc = useQueryClient();
  return useMutation<unknown, Error, number>({
    mutationFn: (scriptId) =>
      relay.delete(`/admin/scripts/${scriptId}?account_id=${aid(accountId)}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: [KEY, "scripts", accountId] }),
  });
}

export function useSaveScriptItems(accountId: string | null) {
  const qc = useQueryClient();
  return useMutation<unknown, Error, { scriptId: number; items: CatalogItemT[] }>({
    mutationFn: ({ scriptId, items }) =>
      relay.put(`/admin/scripts/${scriptId}/items`, { account_id: accountId, items }),
    onSuccess: () => qc.invalidateQueries({ queryKey: [KEY, "scripts", accountId] }),
  });
}

export function useSaveSingles(accountId: string | null) {
  const qc = useQueryClient();
  return useMutation<unknown, Error, CatalogItemT[]>({
    mutationFn: (items) =>
      relay.put(`/admin/catalog/singles`, { account_id: accountId, items }),
    onSuccess: () => qc.invalidateQueries({ queryKey: [KEY, "scripts", accountId] }),
  });
}

export function useImportFolder(accountId: string | null) {
  const qc = useQueryClient();
  return useMutation<{ imported: number }, Error,
    { scriptId: number; folder: string }>({
    mutationFn: ({ scriptId, folder }) =>
      relay.post(`/admin/scripts/import-folder`,
        { account_id: accountId, script_id: scriptId, folder }),
    onSuccess: () => qc.invalidateQueries({ queryKey: [KEY, "scripts", accountId] }),
  });
}

export function usePasteImport(accountId: string | null) {
  const qc = useQueryClient();
  return useMutation<{ imported: number }, Error,
    { scriptId: number; table: string }>({
    mutationFn: ({ scriptId, table }) =>
      relay.post(`/admin/scripts/paste-import`,
        { account_id: accountId, script_id: scriptId, table }),
    onSuccess: () => qc.invalidateQueries({ queryKey: [KEY, "scripts", accountId] }),
  });
}

export function useSimulate(accountId: string | null) {
  return useMutation<SimulateResponse, Error, string>({
    mutationFn: (fanSays) =>
      relay.post(`/admin/scripts/simulate`,
        { account_id: accountId, fan_says: fanSays }),
  });
}

export function useAiChatterSessions(accountId: string | null) {
  return useQuery<{ progress: SessionRow[]; offers: OfferRow[] }>({
    queryKey: [KEY, "sessions", accountId],
    enabled: !!accountId,
    queryFn: () =>
      relay.get(`/admin/ai-chatter/sessions?account_id=${aid(accountId)}`, BG_CTX),
    refetchInterval: 15_000,
    refetchOnWindowFocus: false,
  });
}

export function useCancelOffer(accountId: string | null) {
  const qc = useQueryClient();
  return useMutation<unknown, Error, number>({
    mutationFn: (offerId) =>
      relay.post(`/admin/ai-chatter/offers/${offerId}/cancel`,
        { account_id: accountId }),
    onSuccess: () =>
      qc.invalidateQueries({ queryKey: [KEY, "sessions", accountId] }),
  });
}
