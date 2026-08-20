"use client";

/**
 * useTenantKeys — Setup → Keys, the signed-in AGENCY's own LLM keys.
 *
 *   GET /admin/my-llm-keys   masked status per provider (never the raw key)
 *   PUT /admin/my-llm-keys   { provider: value }; "" clears; send ONLY what
 *                            changed — an absent provider keeps its stored key.
 *
 * Distinct from useSecrets (/admin/secrets), which holds the DEPLOYMENT's house
 * keys. These are yours: every OF account you own bills the key you paste here,
 * so no other agency can spend on your credential and you cannot spend on
 * theirs. Requires a signed-in owner — a chatter session gets a 401.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { relay } from "@/lib/relay";

export interface TenantKeyStatus {
  set: boolean;
  /** "••••" + last four. Rendered as a placeholder, NEVER as a field value —
   *  round-tripping it back would be rejected by the server (and would
   *  otherwise overwrite a working key with its own mask). */
  hint: string;
}

export type TenantKeysStatus = Record<string, TenantKeyStatus>;

/** An account two owners are linked to. Nothing says whose key pays, so the
 *  relay refuses rather than bill the wrong one — that account's AI is stopped
 *  until a link is removed. */
export interface SharedAccount {
  account_id: string;
  nickname: string;
  owners: string[];
}

export interface TenantKeysResp {
  providers: TenantKeysStatus;
  shared_accounts?: SharedAccount[];
}

export function useTenantKeys() {
  return useQuery<TenantKeysResp>({
    queryKey: ["my-llm-keys"],
    queryFn: () => relay.get<TenantKeysResp>("/admin/my-llm-keys"),
    staleTime: 30_000,
  });
}

export function useSaveTenantKeys() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (values: Record<string, string>) =>
      relay.put<TenantKeysResp>("/admin/my-llm-keys", values),
    onSuccess: (data) => qc.setQueryData(["my-llm-keys"], data),
  });
}
