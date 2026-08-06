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

/** One band of the reply-time curve. `up_to_min` is a CUT POINT — the band runs
 *  from the previous row's `up_to_min` (0 for the first) up to this one — so the
 *  shape cannot express a gap or an overlap. `pct` is that band's share. */
export interface PaceBand {
  pct: number;
  up_to_min: number;
}

/** One stage of the ghost cycle: chat for `chat_days`, then go dark for
 *  `ghost_days`. The stages REPEAT from the top, and both are days (halves are
 *  legal — the shipped cycle ends on 2.5). */
export interface GhostStage {
  chat_days: number;
  ghost_days: number;
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
  /** The "deepen" phase — work a mined gen_info detail into an ordinary reply once
   *  the bio-gap list for that fan is empty. `rate` is 0..1 (0.30 = 30% of eligible
   *  replies); at 1.0 every reply to a gathered fan carries a question. */
  profile_openers_enabled?: boolean;
  profile_openers_rate?: number;
  /** Per-signal reply caps within a burst (0 = unlimited). Sent as a COMPLETE
   *  object — the server fills any missing tier from defaults. */
  msg_limits_by_signal?: {
    baseline?: number;
    buying_signal?: number;
    no_signal?: number;
    pic_sent?: number;
  };
  /** Proven-spender cap FLOOR (item 21b): each rule lifts a fan's burst cap when his
   *  PAID spend (PPV unlocks + tips) over the last `days` is >= `min_cents`. Only ever
   *  RAISES the cap — effective cap = max(signal cap, best-matching spend cap). Highest
   *  matching rule wins; [] ⇒ spend never lifts the cap (signal-only). */
  msg_limits_by_spend?: Array<{ days: number; min_cents: number; cap: number }>;
  /** Silence gap (min) that starts a fresh burst for the caps above. */
  session_gap_minutes?: number;
  /** Keep chatting a just-paid fan this long (min); past it with no new spend, hand off. */
  post_purchase_minutes?: number;
  /** A pending unbought offer older than this (min) drops to the short-leash tier. */
  offer_expiry_minutes?: number;
  /** One gentle re-engage nudge if an offered fan goes quiet without buying.
   *  The only cadence piece that SENDS — OFF by default even when cadence is on. */
  nudge_enabled?: boolean;

  /** Sell in the chat (1:1): a price may go in front of a fan who is actually
   *  in the conversation, and never in front of one who isn't replying. This is the 1:1
   *  seller ONLY — the same gate on the mass blast would delete the blast, whose
   *  whole job is reaching fans who are not mid-conversation. OFF by default. */
  qualification_gate_enabled?: boolean;
  /** Attach the ask when the THREAD IS HOT (he's mid-scene and replying) and the model
   *  wrote no offer marker. The offer is emitted by the MODEL, and mostly it declines —
   *  184 replies produced 4 offers on one account. Thread-heat is a 24.3x lift on the
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
  /** Human reply timing — the account sleeps, its delays vary, and it covers a long
   *  gap in its own voice. Needs a timezone on the account. OFF by default. */
  rhythm_enabled?: boolean;
  /** No-sleep pacing: keep the hot/cold/busy delays + short "stepped away" breaks,
   *  but never the overnight sleep — and no timezone needed. OFF by default. */
  rhythm_no_sleep?: boolean;
  /** Sample the reply delay from the editable curve below instead of the archive-
   *  fitted lognormal. OFF by default — the accounts already earning keep theirs. */
  rhythm_pace_buckets?: boolean;
  /** The bands, as CUT POINTS: each row's floor is the row above it. null ⇒ the
   *  shipped 85/10/4/1. Percentages are normalised server-side, so they don't have
   *  to sum to exactly 100 while you're typing. */
  rhythm_pace_curve?: PaceBand[] | null;
  /** The ghost cycle: whole DAYS where the account doesn't answer this fan, on a
   *  repeating schedule. PER FAN — each one runs off his own first message, so the
   *  roster doesn't go dark together. OFF by default. */
  rhythm_ghost_enabled?: boolean;
  /** The stages, repeating. null ⇒ the shipped 3 days on / 1 off, 4 / 2, 5 / 2.5. */
  rhythm_ghost_cycle?: GhostStage[] | null;
  /** Where the sleep window comes from when nothing is overridden: the house night
   *  ("default", 02:00–06:00 local) or this account's own outbound histogram
   *  ("derived"). */
  rhythm_sleep_source?: "default" | "derived";
  /** null ⇒ `rhythm_sleep_source` decides; ["HH:MM","HH:MM"] ⇒ the operator
   *  overrode it, which outranks both sources. */
  sleep_window?: [string, string] | null;
  /** Human TYPING pacing — the gaps BETWEEN the bubbles of one reply, which the
   *  rhythm settings above never touched (they decide when the FIRST bubble lands).
   *  Measured over 120 days of production, 3.0% of our inter-bubble gaps run longer
   *  than 20s, against 26.9% for a human chatter and 56.6% for a fan: she never
   *  stops mid-reply. OFF by default. */
  pacing_enabled?: boolean;
  /** How often a bubble draws a real "she put the phone down" pause (percent).
   *  This is the whole fix — a flat delay added to every bubble measured WORSE than
   *  changing nothing, because it just relocates the pile. Default 30. */
  pacing_drift_pct?: number;
  /** The longest one such pause may run, in seconds. Held inline, so the server
   *  clamps it under the 120s boundary past which a reply must be rescheduled
   *  instead. Default 90. */
  pacing_drift_cap_s?: number;
  /** Blank the "…is typing" bar for 5–10s mid-bubble, so it doesn't run unbroken
   *  for exactly the length of the gap. Adds no delay at all — it only changes what
   *  the fan sees during a wait that was happening anyway. Default ON. */
  pacing_think_gaps?: boolean;
  /** {slot: [lines]} over the shipped script pack. A slot the operator blanked is
   *  dropped on save, so it falls back to the shipped default — never an empty send. */
  script_pack_overrides?: Record<string, string[]>;
  nudge_after_minutes?: number;
}

interface AiChatterConfigResponse {
  account_id: string;
  config: AiChatterConfig;
  defaults: AiChatterConfig;
  /** The SHIPPED lines, {slot: [lines]} — pre-filled in the script-pack editor,
   *  ALREADY resolved for this account's lane. Never assume the female pack: editing
   *  one box stores the whole box, so wrong defaults here get persisted and sent. */
  script_pack: Record<string, string[]>;
  /** {slot: "when it fires"} for the editor's hint line. Server-side because it names
   *  the slots, and the slot schema is the server's (`script_packs.SLOTS`). */
  slot_help: Record<string, string>;
  /** "Load starter pack" — ten prewritten sellable pieces, ALREADY resolved for
   *  this account's lane. Like `script_pack` above, `description_for_ai` is prompt
   *  text the click persists onto the account, so the lane is decided server-side
   *  and the browser holds none of it. */
  starter_singles: CatalogItemT[];
  /** The creator's IANA zone (a COLUMN, not part of the config blob). */
  timezone: string | null;
  /** Legacy whole-hour offset — the fallback when `timezone` is null. */
  utc_offset: number;
  /** Resolved creator-local offset in MINUTES. null ⇒ neither a timezone nor a
   *  non-zero utc_offset is set, so Human Rhythm must stay blocked (a UTC default
   *  would put a US creator to sleep through their best hours). */
  tz_offset_minutes: number | null;
  /** Computed server-side from the account's own outbound hour histogram. */
  derived_sleep_window: [string, string];
  /** What it would ACTUALLY do: the override if set, else whichever source
   *  `sleep_source` names. Resolved server-side so the card and the engine can't
   *  disagree about which window is in force. */
  effective_sleep_window: [string, string];
  default_sleep_window: [string, string];
  /** Which source produced `effective_sleep_window` absent an override. */
  sleep_source: "default" | "derived";
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

/** One row's proposed content. `media_ids` empty + `empty_reason` set means the
 *  vault has nothing that honestly fits that row's caption — which is the
 *  answer, not a failure: an item with no media is never offered. */
export interface SingleSuggestionT {
  /** Null for a draft row that has not been saved yet. */
  id: number | null;
  /** "db:<id>" or "draft:<index>" — how the caller matches a proposal back to
   *  the editor row it came from, since a draft has no id. */
  key?: string;
  label: string | null;
  kind: string;
  description_for_ai: string | null;
  price_cents: number;
  matched: string[];      // which keywords in the row's own text fired
  /** How many vault items match this text AT ALL (before exclusivity). */
  candidates: number;
  /** How many of those were still unsold when this row was considered. */
  available?: number;
  /** Matches the words but not the price — the one empty-row cause an operator
   *  fixes with a number instead of a camera. */
  wrong_price?: number;
  /** The price that WOULD fill this row, when the band is what emptied it. */
  suggested_price_cents?: number;
  /** The fewest media this caption can honestly ship. */
  needs?: number;
  media_ids: number[];
  /** What the row SELLS, before teaser or filler stills are attached. */
  locked_media_ids?: number[];
  /** Stills added to reach one-per-$20 (and 10 on any photo row). May be dups
   *  of what other rows sell — padding is not what the caption promises. */
  padding_media_ids?: number[];
  /** Duplicated to reach the row's minimum, because too few unsold matches
   *  were left. Everything above the minimum stays exclusive unless the
   *  operator ticks ♻️ allow reuse. */
  dup_media_ids?: number[];
  /** Volume shortfall the price implies — runtime or still count. A WARNING:
   *  a small bundle is a weak offer, not a false one, so it still ships. */
  shape_note?: string;
  runtime_seconds?: number;
  runtime_wanted?: number;
  images_wanted?: number;
  /** Goes out UNLOCKED. Always a subset of media_ids — OF unlocks a slice of
   *  the attachment, so a borrowed teaser is attached to the item too. */
  preview_media_ids: number[];
  /** The teaser is a still another row also sells (nothing unsold was safe to
   *  give away). */
  preview_shared?: boolean;
  empty_reason: string;
}

/** Propose vault media for singles that have TEXT but no CONTENT. Read-only —
 *  the caller patches rows locally and the operator still presses Save. */
export function useSuggestSingles(accountId: string | null) {
  return useMutation<
    { proposals: SingleSuggestionT[]; summary: Record<string, number> },
    Error,
    { onlyEmpty?: boolean; drafts?: DraftRowT[]; allowReuse?: boolean } | void
  >({
    // POST, not GET: rows added by "Suggest text sets" are not saved yet, so
    // the server cannot see them unless they ride along in the body.
    mutationFn: (vars) =>
      relay.post(`/admin/catalog/singles/suggest`, {
        account_id: accountId,
        only_empty: (vars && vars.onlyEmpty) !== false,
        drafts: (vars && vars.drafts) ?? [],
        allow_reuse: (vars && vars.allowReuse) === true,
      }),
  });
}

/** An unsaved editor row, as the fill endpoint needs to see it. */
export interface DraftRowT {
  label: string;
  description_for_ai: string;
  kind: string;
  /** Not cosmetic: the price band is the only thing standing between a $200
   *  caption and a lingerie tease, and a draft posted without one arrives at 0
   *  — ungated on exactly the rows the suggest button just invented. */
  price_cents: number;
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

/** One proposed CAPTION row — text, price and kind, no media. `fillable` is how
 *  many unbound vault items the allocator matched to it, so a proposal is only
 *  offered when this vault can honestly fill it. */
export interface TextSuggestionT {
  label: string;
  description_for_ai: string;
  /** Same union as CatalogItemT — the rows drop straight into the singles
   *  editor, and the server only ever emits these three. */
  kind: CatalogItemT["kind"];
  price_cents: number;
  floor_cents: number;
  fillable: number;
  needs: number;
  previews?: number;
  why?: string;
  /** Set on AI-written lines: which media group the line was written from. */
  source?: string;
  media_ids?: number[];
}

/** Propose caption rows to ADD. The other half of `useSuggestSingles`: that one
 *  fills content into a row that already has text, this one produces the text.
 *  Read-only — the caller appends rows locally and the operator still presses
 *  Save singles. `blocked` is set when the vault has no V2 describe fields, in
 *  which case the answer is "run Describe", not a content gap. */
export function useSuggestTexts(accountId: string | null) {
  return useMutation<
    {
      proposals: TextSuggestionT[];
      shoot: TextSuggestionT[];
      blocked: string;
      summary: Record<string, number>;
    },
    Error,
    void
  >({
    mutationFn: () =>
      relay.get(`/admin/catalog/singles/suggest-texts?account_id=${aid(accountId)}`),
  });
}

/** Write NEW caption lines from the vault's own vision facts — the sibling of
 *  `useSuggestTexts`, which only ever offers the same 14 shipped captions.
 *  Costs one cheap LLM call per media group, so it is a POST and the operator
 *  presses it deliberately. Same row shape, so the same pick-list handles both. */
export function useGenerateLines(accountId: string | null) {
  return useMutation<
    {
      proposals: TextSuggestionT[];
      shoot: TextSuggestionT[];
      blocked: string;
      summary: Record<string, number>;
      errors: string[];
    },
    Error,
    { limit?: number } | void
  >({
    mutationFn: (vars) =>
      relay.post(`/admin/catalog/singles/generate-lines`, {
        account_id: accountId, limit: (vars && vars.limit) ?? 8,
      }),
  });
}
