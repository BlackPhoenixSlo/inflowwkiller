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
  /** IANA zone (e.g. America/Vancouver). Wins over utc_offset; DST-correct. */
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
  defaults: BrainConfig;    // one account-derived starter brain (no images) — seeds blank
                            // accounts and backs the "Reset to defaults" button
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
    onSuccess: () => qc.invalidateQueries({ queryKey: ["account-config", accountId] }),
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
  /** Resolved IN CODE from the city/country, never model-guessed. null = ambiguous. */
  timezone: string | null;
  timezone_changed: boolean;
  current_timezone: string | null;
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
