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
  /** Also engage fans flagged `old_fan_pre_ai` (onboarded before the AI) —
   *  mostly pure convo, with an info question ~every old_fan_question_every replies. */
  engage_old_fans?: boolean;
  old_fan_question_every?: number;
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

  /** Sell in the chat (1:1): she may put a price in front of a fan who is actually
   *  talking to her, and never in front of one who isn't replying. This is the 1:1
   *  seller ONLY — the same gate on the mass blast would delete the blast, whose
   *  whole job is reaching fans who are not mid-conversation. OFF by default. */
  qualification_gate_enabled?: boolean;
  /** Attach the ask when the THREAD IS HOT (he's mid-scene and replying) and the model
   *  wrote no offer marker. The offer is emitted by the MODEL, and mostly it declines —
   *  184 replies produced 4 offers on sakai. Thread-heat is a 24.3x lift on the
   *  purchase; the gate alone is ~1x. Rides the gate + all the brakes. OFF by default. */
  force_ask?: boolean;
  /** The FLOOR: after this many of HIS messages with no ask in front of him (ours or a
   *  human chatter's), ask anyway — for the men who never turn the chat sexual and so
   *  would otherwise be chatted to for free forever. 0 = off. Brakes still apply. */
  ask_after_fan_msgs?: number;
  /** Content-derived price bands + the post-purchase ladder. Requires the gate
   *  above; the server forces it back off without it. OFF by default. */
  smart_pricing_enabled?: boolean;
  /** Hard takeover: once a fan is in an active sale, the seller drives his thread
   *  regardless of the base chatter mode (bypasses backup-SLA + closer-skip), then
   *  hands back via the cooldown. Inert unless the gate is on. */
  upsell_takes_over?: boolean;
  /** After a buy, one unsolicited priced follow-up rung (engages a silent buyer). */
  post_buy_rung_enabled?: boolean;
  /** A genuinely FREE unseen-media thank-you at aftercare after >=2 paid rungs. */
  gift_enabled?: boolean;
  /** The "im filming it rn" active fiction — chargeback surface, 0-EV. OFF default. */
  filming_stall_enabled?: boolean;
  /** 1 = tap out after one unpaid rung; 2 = one win-back discount, then stop. */
  stop_after_unpaid_rungs?: number;
  /** Rolling 7-day paid brake → companion for the window (cents). */
  spend_velocity_cap_7d_cents?: number;
  /** Ladder step off his last paid rung (default 1.75x). The item order drives most
   *  of the climb; this is the per-rung lift. */
  escalation_mult?: number;
  /** Never ask more than this multiple of his biggest-ever single PPV (default 3x).
   *  Raising past 3x is the "to the moon" lever — conversion drops past it. */
  max_ask_history_mult?: number;
  /** Human reply timing — she sleeps, her delays vary, and she covers a long gap
   *  in her own voice. Needs a timezone on the account. OFF by default. */
  rhythm_enabled?: boolean;
  /** No-sleep pacing: keep the hot/cold/busy delays + short "stepped away" breaks,
   *  but never the overnight sleep — and no timezone needed. OFF by default. */
  rhythm_no_sleep?: boolean;
  /** null ⇒ DERIVED from the account's own outbound history (the server hands the
   *  derived window back); ["HH:MM","HH:MM"] ⇒ the operator overrode it. */
  sleep_window?: [string, string] | null;
  /** {slot: [lines]} over the shipped script pack. A slot the operator blanked is
   *  dropped on save, so it falls back to the shipped default — never an empty send. */
  script_pack_overrides?: Record<string, string[]>;
  nudge_after_minutes?: number;
}

interface AiChatterConfigResponse {
  account_id: string;
  config: AiChatterConfig;
  defaults: AiChatterConfig;
  /** The SHIPPED lines, {slot: [lines]} — pre-filled in the script-pack editor. */
  script_pack: Record<string, string[]>;
  /** The creator's IANA zone (a COLUMN, not part of the config blob). */
  timezone: string | null;
  /** Legacy whole-hour offset — the fallback when `timezone` is null. */
  utc_offset: number;
  /** Resolved creator-local offset in MINUTES. null ⇒ neither a timezone nor a
   *  non-zero utc_offset is set, so Human Rhythm must stay blocked (a UTC default
   *  would put a US creator to sleep through her best hours). */
  tz_offset_minutes: number | null;
  /** Computed server-side from the account's own outbound hour histogram. */
  derived_sleep_window: [string, string];
  /** The override if one is set, else the derived window — what she'd actually do. */
  effective_sleep_window: [string, string];
  default_sleep_window: [string, string];
}

/** The config blob + the `timezone` COLUMN travel together: the operator sets the
 *  timezone from the same Human Rhythm section, and `timezone: undefined` means
 *  "leave it alone" (so every other save path keeps working untouched). */
export interface SaveAiChatterConfigVars {
  config: AiChatterConfig;
  timezone?: string | null;
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
  return useMutation<unknown, Error, SaveAiChatterConfigVars>({
    mutationFn: ({ config, timezone }) =>
      relay.put(`/admin/ai-chatter-config`, {
        account_id: accountId,
        config,
        // Omitted entirely when undefined — the server reads "absent" as
        // "leave the column alone", so a save from any other surface can't
        // blank a timezone it never showed.
        ...(timezone === undefined ? {} : { timezone }),
      }),
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
