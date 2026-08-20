"use client";

/**
 * useSecrets — the Setup → Keys store.
 *
 *   GET /admin/secrets   masked status of every UI-settable key (never the raw
 *                        value): { keys: { NAME: SecretKeyStatus } }
 *   PUT /admin/secrets   flat body { NAME: value }; "" clears. Returns the new
 *                        masked status.
 *
 * Values are stored server-side in service/secrets/secrets.json and take effect on the
 * next call — no relay restart.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { relay } from "@/lib/relay";

export interface SecretKeyStatus {
  label: string;
  group: string;
  secret: boolean;
  multiline: boolean;
  help: string;
  set: boolean;
  source: "store" | "env" | "unset";
  hint: string;
}

export type SecretsStatus = Record<string, SecretKeyStatus>;
export interface SecretsResp {
  keys: SecretsStatus;
}

export function useSecrets() {
  return useQuery<SecretsResp>({
    queryKey: ["secrets"],
    queryFn: () => relay.get<SecretsResp>("/admin/secrets"),
    staleTime: 30_000,
  });
}

export function useSaveSecrets() {
  const qc = useQueryClient();
  return useMutation({
    // values: NAME -> new value ("" clears). Only send the keys you changed.
    mutationFn: (values: Record<string, string>) =>
      relay.put<SecretsResp>("/admin/secrets", values),
    onSuccess: (data) => qc.setQueryData(["secrets"], data),
  });
}
