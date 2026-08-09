"use client";

/**
 * useAccountConfig — the data layer for the per-account "Brain" editor
 * (account_ai_config) on /automations. Reads/writes the owner-gated
 * /admin/account-config routes (covered by the /admin/:path* rewrite).
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { relay, type RelayContext } from "@/lib/relay";

const BG_CTX: RelayContext = { priority: "background" };

export interface BrainConfig {
  persona: string | null;
  location: string | null;
  /**
   * Structured creator canon pinned into every chat prompt — the facts she must
   * never contradict. Keys: age, job, home_city, home_country, born_city,
   * born_country, upbringing, living_situation, relationship, family, pets.
   * Empty {} = never enriched; nothing renders into the prompt.
   */
  persona_facts: Record<string, string>;
  /** ISO 639-1 the creator writes in; "en" default. Gates output language + guard vocab. */
  language: string;
  /**
   * Whose voice this account writes in: "her" (every account that has ever run
   * here) or "him" (the male lane). Set from the Brain panel's Creator dropdown,
   * and it MUST stay on this type and travel with the form, because
   * Save PUTs the whole config object back. A field the type doesn't know about
   * is a field a rebuild silently drops, and dropping this one puts a male
   * creator back in the female lane mid-conversation.
   */
  voice: string;
  /** LEGACY IANA zone (e.g. America/Vancouver). No longer settable from the Brain —
   *  `utc_offset` is the account's one clock, and this is only consulted when that
   *  is unset. Saving the Brain clears it. */
  timezone: string | null;
  utc_offset: number;
  daily_cost_cap_cents: number;
  model: string | null;
  model_by_purpose: Record<string, string>;
  time_activities: Record<string, string>;
  time_images: Record<string, number>;
}

/** One creator-canon field, as served by the API — the single source of truth. */
export interface PersonaFactField {
  key: string;
  label: string;
  /** Enrich never proposes these: a business decision, or photographic evidence. */
  operator_only: boolean;
  /** Editor hint. Server-side so the 26 keys are enumerated exactly once. */
  placeholder: string;
}

export interface LanguageOption {
  code: string;
  label: string;
}

export interface AccountConfigResp {
  account_id: string;
  config: BrainConfig;
  defaults: BrainConfig;    // starter brain for the STORED lane (no images) — seeds
                            // a blank account, which has no unsaved dropdown yet
  /** Both lanes' starter brains. "Reset to defaults" reads this by the SELECTED
   *  voice: the dropdown is local form state until Save, so resolving the lane from
   *  the stored column refilled a male account with the female brain. */
  defaults_by_voice: Record<string, BrainConfig>;
  /** The canon field contract per lane. Read by the SELECTED voice for the same
   *  reason as `defaults_by_voice`: the labels are what the operator types AGAINST,
   *  and a female label is an instruction to write female canon. */
  persona_fact_fields_by_voice: Record<string, PersonaFactField[]>;
  slots: string[];          // the 6 time-of-day slot keys, ordered
  model_options: string[];  // LLM model ids the account may pick
  purposes: string[];       // per-purpose override targets
  languages: LanguageOption[]; // language codes + labels for the dropdown
  /** Creator-canon fields, ordered by how often fans ask. Single source of truth. */
  persona_fact_fields: PersonaFactField[];
}

export function useAccountConfig(accountId: string | null) {
  return useQuery<AccountConfigResp>({
    queryKey: ["account-config", accountId],
    enabled: !!accountId,
    queryFn: () =>
      relay.get<AccountConfigResp>(
        `/admin/account-config?account_id=${encodeURIComponent(accountId!)}`,
        BG_CTX,
      ),
    staleTime: 30_000,
    refetchOnWindowFocus: false,
  });
}

export function useSaveAccountConfig(accountId: string | null) {
  const qc = useQueryClient();
  return useMutation<{ account_id: string; config: BrainConfig }, Error, BrainConfig>({
    mutationFn: (config) =>
      relay.put("/admin/account-config", { account_id: accountId, config }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["account-config", accountId] });
      // …AND the seller payload, which is DERIVED from the lane this write moves.
      // `GET /admin/ai-chatter-config` resolves `script_pack` and `starter_singles`
      // from `account_ai_config.voice` server-side, but that query is keyed
      // ["ai-chatter","config",id] with staleTime 60s — and BrainPanel (the Creator
      // dropdown) and the seller tabs are mounted on the SAME page. So flipping an
      // account to Male left the seller tab serving the female pack for the rest of
      // the session, and "Load starter pack" then PERSISTED the female
      // `description_for_ai` pitch contracts onto a male account. The prefix form
      // also catches ["ai-chatter","scripts",id] — the Singles list that button
      // writes into. `useSettingsTransfer.settingsQueryKeys` already pairs these two
      // for exactly this reason; the narrower writers just never followed it.
      qc.invalidateQueries({ queryKey: ["ai-chatter"] });
    },
  });
}


/** The 🪄 Enrich proposal — what the empty canon slots could be filled with. */
export interface EnrichResult {
  account_id: string;
  /** Facts already locked in; the model may not overwrite these. */
  known: Record<string, string>;
  /** Only the newly filled slots. */
  proposed: Record<string, string>;
  /** known + proposed — what Save would store. */
  facts: Record<string, string>;
  /** Resolved IN CODE from the city/country, never model-guessed. null = ambiguous.
   *  Names her PLACE; the clock the account stores is the offset below. */
  timezone: string | null;
  timezone_changed: boolean;
  current_timezone: string | null;
  /** THE clock to write — that place's whole-hour offset right now. null = her place
   *  is ambiguous (or a half-hour zone the integer column can't hold), so the
   *  operator picks it from the dropdown instead. */
  utc_offset: number | null;
  utc_offset_changed: boolean;
}

/**
 * Propose values for the empty creator-canon slots. PROPOSES ONLY — the operator
 * reviews and saves through the normal PUT, so nothing reaches a fan-facing
 * prompt unread.
 */
export function useEnrichPersona() {
  return useMutation<EnrichResult, Error, { account_id: string; hint?: string | null }>({
    mutationFn: (vars) => relay.post("/admin/account-config/enrich", vars),
  });
}
