"use client";

/**
 * useChatImageDesc — map of inbound message_id → what the AI saw in the photo
 * (or gif) the fan sent.
 *
 * Backs the "👁 …" caption under an incoming image bubble: the chatter sees
 * exactly the text the AI reads as "[he sent: …]" when it writes the
 * reply, so a bad read is visible instead of silently steering the answer.
 *
 * An OVERLAY keyed by message id (same shape as useChatAttribution) rather than
 * a field on the message row: the thread renders LIVE OF payloads, which carry
 * no image_desc — only an id-keyed side map re-attaches to whichever copy of the
 * bubble is on screen.
 *
 * Unlike the attribution overlay it does NOT window on the oldest visible id.
 * Attribution has to: every outbound bubble can carry a "sent by" label, so its
 * map grows with the thread. Descriptions only ever exist for INBOUND media, and
 * the endpoint's 200-row cap swallows the real distribution whole — measured on
 * prod, the busiest fan alive has 50 media DMs and the mean is 2.7. Dropping the
 * anchor from the key is what makes this cache PERSISTABLE: a windowed key mints
 * a fresh entry (each a superset of the last) every time you scroll back, so the
 * localStorage snapshot would carry five near-duplicate copies of one fan's map.
 * One stable key per fan = one entry, and a reload/popout renders captions with
 * no round-trip at all. See PERSIST_PREFIXES in providers.tsx.
 *
 * staleTime stays short-ish because a describe lands seconds AFTER the photo row
 * does (the ingest hook writes the column post-insert), so an aggressively cached
 * map would show a blank caption on the newest picture.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { relay } from "@/lib/relay";
import { type FanId } from "@/lib/fanId";

export interface ImageDescResponse {
  by_msg_id: Record<string, string>;
}

export function useChatImageDesc(accountId: string | null, fanId: FanId | null) {
  return useQuery<ImageDescResponse>({
    queryKey: ["msg-image-desc", accountId, fanId],
    enabled: !!accountId && fanId != null,
    // No `since_id`: the whole fan's map is one small response (server caps at
    // 200; prod's worst fan has 50). Asking for all of it keeps the key stable.
    queryFn: () =>
      relay.get<ImageDescResponse>(
        `/admin/messages/${encodeURIComponent(accountId!)}/${fanId}/image-desc`,
      ),
    staleTime: 30 * 1000,
    refetchOnWindowFocus: false,
  });
}

/** Describe / re-read ONE inbound photo on demand — one vision call (~$0.0004),
 *  so it's wired to an explicit click, never a hover.
 *
 *  Covers the two cases the ingest hook structurally can't: a photo that landed
 *  before the feature shipped (or while the flag was off / the LLM cap was hit)
 *  has a NULL column forever, and a read the model got wrong needs a re-run.
 *  Patches the overlay cache in place on success so the caption updates without
 *  a refetch round-trip. */
export function useDescribeImage(accountId: string | null, fanId: FanId | null) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (vars: { messageId: number; force?: boolean }) => {
      // No `enabled` gate exists on a mutation, so the invariant is stated here
      // rather than asserted away with a `!` — otherwise a null account would
      // POST to /admin/messages/null/… and 404 with a confusing message.
      if (!accountId || fanId == null) {
        throw new Error("describe needs an account + fan");
      }
      const qs = vars.force ? "?force=true" : "";
      return relay.post<{ image_desc: string | null }>(
        `/admin/messages/${encodeURIComponent(accountId)}/${fanId}/${vars.messageId}/describe${qs}`,
        {},
      );
    },
    onSuccess: (data, vars) => {
      if (!data?.image_desc) return;
      qc.setQueriesData<ImageDescResponse>(
        { queryKey: ["msg-image-desc", accountId, fanId] },
        (prev) => ({
          by_msg_id: { ...(prev?.by_msg_id ?? {}), [String(vars.messageId)]: data.image_desc! },
        }),
      );
    },
  });
}
