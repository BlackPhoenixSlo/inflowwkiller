"use client";

/**
 * useSendMessage — optimistic send for a single chat.
 *
 * Flow:
 *   1. Caller invokes send(text, opts). We:
 *      a. Allocate a fresh negative tempId.
 *      b. Build an optimistic OFMessage and append via appendLocal.
 *      c. POST /api/of/v2/chats/{fanId}/messages.
 *      d. On success → reconcileLocal(tempId, server) (merges media files).
 *      e. On failure → failLocal(tempId) (shows Retry button).
 *
 * Cross-chat guard: we capture `accountId` + `fanId` at send time. If the
 * user switches chats while a send is in flight, the response still
 * reconciles into the original chat's query — there's no `selectedChatIdRef`
 * issue because TanStack Query's query key is the source of truth (we
 * write straight to the (accountId, fanId) cache, regardless of what's
 * visible).
 *
 * Inflight counter exposed for the spinner.
 */

import { useCallback, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { relay, RelayError, type OFChatItem, type OFMedia, type OFMessage, type SendMessageBody, type VaultMedia } from "@/lib/relay";
import { type FanId } from "@/lib/fanId";
import { parseSendFailure, type SendFailure } from "@/lib/sendFailure";
import { useEmployee } from "@/contexts/EmployeeContext";
import {
  useRosterCountActions,
  rosterDelta,
  applyRosterDelta,
  rosterPriorFromRow,
} from "@/hooks/useRosterCounts";
import { findFanChatRow } from "@/hooks/useInboxRealtime";

import { useChatMessages } from "./useChatMessages";
import { scheduledSendsKey } from "./useServerScheduledSends";

export interface UseSendMessageOpts {
  accountId: string | null;
  fanId: FanId | null;
  /** Our own creator id — numeric on OF, a string snowflake on Fansly. */
  fromUserId?: number | string;
  fromUserName?: string;
  hookHandle: ReturnType<typeof useChatMessages>;
}

interface SendOpts {
  text: string;
  price?: number;
  lockedText?: boolean;
  /** Attached vault items. Their ids form the wire payload; their thumb
   *  URLs seed the optimistic bubble so the sender sees their picks
   *  instantly + survives reconcile via mergeMedia. */
  attached?: VaultMedia[];
  /** Vault ids (subset of `attached`) sent unlocked when `price > 0`.
   *  Empty / omitted = everything locked. */
  previews?: number[];
  /** ISO 8601 timestamp. If set + future, the message is queued via
   *  /messages/scheduled instead of /messages — no optimistic bubble. */
  scheduledAt?: string;
  /** OF native quote-reply target id. Threaded into both the wire body
   *  and the optimistic bubble so the sender sees their reply context
   *  immediately. */
  replyToMessageId?: number | string;
  /** Optimistic-only snapshot of the quoted message — used so the
   *  outgoing bubble can render a quote card before the server response
   *  comes back populated. */
  replyToMessage?: OFMessage["replyToMessage"];
  /** Numeric OF user ids of creators to @-tag (sent as `userTags`).
   *  Source: TagCreatorsPicker — never user-typed. */
  taggedUsers?: number[];
  /** Giphy id (e.g. "0ndspgUFbm9iQtyX2Q"). When set, OF treats the send
   *  as a GIF message — `text` typically empty, `media_files` empty,
   *  `giphyId` rides at the top level. One GIF per send. */
  giphyId?: string;
}

function vaultToOFMedia(v: VaultMedia): OFMedia {
  // Local preview (just-uploaded file) wins over server URLs because the
  // CDN hasn't seen the file yet — and for fresh claims there's no `files`
  // at all, so without the blob URL the bubble would be blank.
  const thumbUrl =
    v._localPreview || v.files?.thumb?.url || null;
  const sourceUrl =
    v._localPreview ||
    v.files?.full?.url ||
    v.files?.preview?.url ||
    null;
  return {
    id: v.id,
    type: v.type,
    files: {
      thumb: thumbUrl ? { url: thumbUrl } : null,
      source: sourceUrl ? { url: sourceUrl } : null,
    },
  };
}

export function useSendMessage(opts: UseSendMessageOpts) {
  const { accountId, fanId, fromUserId, fromUserName, hookHandle } = opts;
  const { appendLocal, reconcileLocal, failLocal, dropLocal } = hookHandle;
  const qc = useQueryClient();
  const { current: currentEmployee } = useEmployee();
  const employeeId = currentEmployee?.id ?? null;
  const { patchLocal: patchRosterLocal, refreshOne: refreshRoster } = useRosterCountActions();

  // Decrementing temp-id counter (negative ids never collide with OF).
  const nextTempIdRef = useRef(-1);
  // Stash the wire body + attached vault metadata per tempId so Retry
  // can replay both the request and re-seed the optimistic bubble.
  const payloadByTempIdRef = useRef<Map<number, { body: SendMessageBody; attached: VaultMedia[] }>>(new Map());
  const [inflight, setInflight] = useState(0);

  const doSend = useCallback(
    async (tempId: number, body: SendMessageBody, capturedAccountId: string, capturedFanId: FanId) => {
      setInflight((n) => n + 1);
      try {
        const server = await relay.post<OFMessage>(
          `/api/of/v2/chats/${capturedFanId}/messages`,
          body,
          { accountId: capturedAccountId, employeeId },
        );
        reconcileLocal(tempId, server);
        // Record vault sends so the picker can mark "you sent this" /
        // "she bought this" next time. Pull ids straight from OF's response
        // because (a) fresh-claims didn't have a real vault id pre-send,
        // and (b) some uploads collapse onto an existing vault item.
        recordVaultSends(qc, capturedAccountId, capturedFanId, server, body.price ?? 0);
        // Invalidate the outbound-attribution map so the new real
        // message_id picks up its "Sent by …" label. Without this,
        // the optimistic fallback (`_tempId<0`) carries the label
        // until the next chat refetch replaces the merged row with
        // pure server data (no `_tempId`) — at which point the label
        // disappears for the 5-min attribution staleTime window.
        qc.invalidateQueries({ queryKey: ["msg-attribution", capturedAccountId, capturedFanId] });
        payloadByTempIdRef.current.delete(tempId);
        return true;
      } catch (err) {
        // Leave the optimistic in place but flag it failed so Retry can
        // recover, carrying what OF (or our own pre-flight guard) said and
        // which attachments it blamed — one parse of one envelope, see
        // lib/sendFailure. The console line spreads the upstream details so
        // logs read as status + body, not "[object Object]".
        let failure: SendFailure;
        if (err instanceof RelayError) {
          console.warn("[send] failed", { status: err.status, body: err.body, message: err.message });
          const parsed = parseSendFailure(err.body);
          failure = { ...parsed, reason: parsed.reason ?? `HTTP ${err.status}` };
        } else {
          console.warn("[send] failed", err);
          failure = { reason: (err as Error)?.message };
        }
        failLocal(tempId, failure);
        // The optimistic roster decrement in the send path assumed this would
        // land; it didn't → reconcile the badge to OF truth (force past the 8s
        // cooldown — this only fires on the rare failure path).
        void refreshRoster(capturedAccountId, { force: true });
        return false;
      } finally {
        setInflight((n) => n - 1);
      }
    },
    [failLocal, reconcileLocal, qc, employeeId, refreshRoster],
  );

  // Immediate-send path, extracted so the no-schedule case (and retry) can
  // call it without recursion gymnastics.
  const sendNow = useCallback(
    async (input: SendOpts, capturedAccountId: string, capturedFanId: FanId) => {
      const attached = input.attached ?? [];
      const body: SendMessageBody = {
        text: input.text,
        locked_text: input.lockedText ?? false,
        price: input.price ?? 0,
        media_files: attached.map((m) => m._claim ?? m.id),
      };
      if (input.previews && input.previews.length > 0 && (input.price ?? 0) > 0) {
        body.previews = input.previews;
      }
      if (input.replyToMessageId) body.reply_to_message_id = input.replyToMessageId;
      if (input.taggedUsers && input.taggedUsers.length > 0) {
        body.tagged_users = input.taggedUsers;
      }
      if (input.giphyId) body.giphy_id = input.giphyId;
      const tempId = nextTempIdRef.current--;
      payloadByTempIdRef.current.set(tempId, { body, attached });
      const optimisticMedia: OFMedia[] = attached.map(vaultToOFMedia);
      const optimistic: OFMessage = {
        id: tempId,
        _tempId: tempId,
        _pending: true,
        text: input.text,
        fromUser: { id: fromUserId ?? -1, name: fromUserName ?? "you" },
        createdAt: new Date().toISOString(),
        media: optimisticMedia,
        mediaCount: optimisticMedia.length,
        price: input.price ?? 0,
        replyToMessageId: input.replyToMessageId,
        replyToMessage: input.replyToMessage ?? null,
        giphyId: input.giphyId,
      };
      appendLocal(optimistic);
      // Optimistically flip the chat-list row's `lastMessage.fromUser` to
      // us so the yellow "read but unanswered" dot disappears immediately.
      // Without this the dot lingers for up to 60s (the chat-list poll).
      // Read the fan's PRE-flip roster state from the FRESHEST cached row
      // (findFanChatRow picks newest lastMessage, not a stale first-match) BEFORE
      // patchChatListAfterSend flips the row to us.
      const prior = rosterPriorFromRow(findFanChatRow(qc, capturedAccountId, capturedFanId));
      patchChatListAfterSend(qc, capturedAccountId, capturedFanId, {
        // The raw account id, NOT Number(accountId): a Fansly snowflake is past
        // 2^53, so parsing it yields a DIFFERENT id — the row would read as "the
        // fan spoke last" and the yellow dot would come straight back on the
        // thread we just answered.
        fromUserId: fromUserId ?? capturedAccountId,
        fromUserName: fromUserName ?? "you",
        text: input.text,
        mediaCount: optimisticMedia.length,
        price: input.price ?? 0,
      });
      // Clear this conversation's roster badge (orange owe-reply, or blue if it
      // was still unread) the SAME tick we send. The outbound SSE echo can't do
      // this — by the time it lands, the row above is already flipped to us, so
      // the realtime handler would compute a no-op. Must happen here, from the
      // prior state, or the manual-send badge waits out the 60s poll.
      if (prior) {
        const d = rosterDelta(true, prior);
        if (d.dUnread !== 0 || d.dOwe !== 0) {
          patchRosterLocal(capturedAccountId, (cur) => applyRosterDelta(cur, d));
        }
      }
      await doSend(tempId, body, capturedAccountId, capturedFanId);
    },
    [fromUserId, fromUserName, appendLocal, doSend, qc, patchRosterLocal],
  );

  const send = useCallback(
    async (input: SendOpts) => {
      if (!accountId || fanId == null) {
        console.warn("[send] missing accountId or fanId — refusing to send");
        return;
      }

      // Scheduled path. Two branches, BOTH server-side (survive reload + are
      // shared across every chatter on the account):
      //   • Short delay (≤ 15 min from now): hand to OUR executor — enqueue a
      //     `scheduled_jobs` row (kind="scheduled_send") that the supervisor
      //     fires at run_at. We fire it ourselves (not OF's queue) because OF's
      //     queue lags/rejects too-near times; this also lets us attribute the
      //     eventual bubble to the chatter who scheduled it.
      //   • Long delay: hand off to OF's native queue so it survives even if our
      //     VPS is down, can be edited from OF web, etc.
      if (input.scheduledAt) {
        const fireAt = new Date(input.scheduledAt).getTime();
        const delay = fireAt - Date.now();
        if (delay > 0 && delay <= 15 * 60 * 1000) {
          const attached = input.attached ?? [];
          try {
            await relay.post(
              `/api/of/v2/scheduled-sends`,
              {
                fan_id: fanId,
                run_at: input.scheduledAt,
                text: input.text,
                price: input.price ?? 0,
                locked_text: input.lockedText ?? false,
                media_files: attached.map((m) => m._claim ?? m.id),
                previews:
                  input.previews && (input.price ?? 0) > 0 ? input.previews : [],
                tagged_users: input.taggedUsers ?? [],
                giphy_id: input.giphyId,
              },
              { accountId, employeeId },
            );
            // Surface the ghost bubble immediately (and for any other chatter
            // on next poll). Server is the source of truth from here on.
            qc.invalidateQueries({ queryKey: scheduledSendsKey(accountId, fanId) });
          } catch (err) {
            // RETHROW. A scheduled send has no optimistic bubble to turn red —
            // the only evidence it exists is the server-side ghost row. Swallowing
            // here left the composer showing its "Scheduled ✓" toast over an empty
            // textarea for a message the relay had REFUSED (a Fansly PPV/media
            // schedule 400s), which is indistinguishable from success. The caller
            // restores the composer and shows the real reason.
            console.warn("[schedule] near-term enqueue failed", err);
            throw err;
          }
          return;
        }
        const attached = input.attached ?? [];
        try {
          const scheduledBody: Record<string, unknown> = {
            text: input.text,
            locked_text: input.lockedText ?? false,
            price: input.price ?? 0,
            media_files: attached.map((m) => m._claim ?? m.id),
            scheduled_date: input.scheduledAt,
          };
          if (input.previews && input.previews.length > 0 && (input.price ?? 0) > 0) {
            scheduledBody.previews = input.previews;
          }
          if (input.taggedUsers && input.taggedUsers.length > 0) {
            scheduledBody.tagged_users = input.taggedUsers;
          }
          if (input.giphyId) scheduledBody.giphy_id = input.giphyId;
          await relay.post(
            `/api/of/v2/chats/${fanId}/messages/scheduled`,
            scheduledBody,
            { accountId, employeeId },
          );
        } catch (err) {
          // Same contract as the near-term branch above: no bubble exists to
          // carry the failure, so it must reach the composer.
          console.warn("[schedule] failed", err);
          throw err;
        }
        return;
      }

      await sendNow(input, accountId, fanId);
    },
    [accountId, fanId, sendNow, employeeId],
  );

  const retry = useCallback(
    async (tempId: number) => {
      if (!accountId || fanId == null) return;
      const stash = payloadByTempIdRef.current.get(tempId);
      if (!stash) return;
      const { body, attached } = stash;
      const optimisticMedia: OFMedia[] = attached.map(vaultToOFMedia);
      // We DON'T drop+re-append: the bubble stays put, just stop being red.
      // appendLocal is a no-op when the id already exists, so to actually
      // flip the flags we patch via dropLocal+append.
      dropLocal(tempId);
      hookHandle.appendLocal({
        id: tempId,
        _tempId: tempId,
        _pending: true,
        _failed: false,
        text: body.text,
        fromUser: { id: fromUserId ?? -1, name: fromUserName ?? "you" },
        createdAt: new Date().toISOString(),
        media: optimisticMedia,
        mediaCount: optimisticMedia.length,
        price: body.price ?? 0,
        replyToMessageId: body.reply_to_message_id,
      });
      // A successful RETRY (the first attempt failed → D4's catch restored the
      // badge) lands the send but took no send-time decrement — reconcile the
      // badge to OF truth so the answered fan's orange/blue clears without waiting
      // out the 60s poll.
      const ok = await doSend(tempId, body, accountId, fanId);
      if (ok) void refreshRoster(accountId, { force: true });
    },
    [accountId, fanId, fromUserId, fromUserName, hookHandle, doSend, dropLocal, refreshRoster],
  );

  const cancelOptimistic = useCallback(
    (tempId: number) => {
      dropLocal(tempId);
      payloadByTempIdRef.current.delete(tempId);
    },
    [dropLocal],
  );

  return { send, retry, cancelOptimistic, inflight };
}

/** Record vault sends in the local DB so the picker shows "sent / paid"
 *  badges on this fan's next vault open. Fire-and-forget: the request is
 *  pure-local, but failing to log a send shouldn't surface as an error
 *  toast (the message went out OK from OF's perspective). We pull media
 *  ids from the SERVER response, not the optimistic body, because:
 *    • Fresh-claim uploads only get a real id after OF assigns one.
 *    • Some uploads collapse onto an existing vault item, in which case
 *      OF echoes the canonical id we want to track. */
function recordVaultSends(
  qc: ReturnType<typeof useQueryClient>,
  accountId: string,
  fanId: FanId,
  server: OFMessage,
  priceDollars: number,
): void {
  const mediaIds = (server.media ?? [])
    .map((m) => (typeof m.id === "number" ? m.id : Number(m.id)))
    .filter((n) => Number.isFinite(n) && n > 0);
  if (mediaIds.length === 0) return;
  const msgId = typeof server.id === "number" ? server.id : Number(server.id);
  relay
    .post("/admin/vault/sends", {
      account_id: accountId,
      fan_id: fanId,
      media_ids: mediaIds,
      message_id: Number.isFinite(msgId) && msgId > 0 ? msgId : null,
      price_cents: Math.max(0, Math.round((priceDollars || 0) * 100)),
    })
    .then(() => {
      qc.invalidateQueries({ queryKey: ["vault-history", accountId, fanId] });
    })
    .catch((err) => console.warn("[vault-sends] record failed", err));
}

/** Patch the chat-list cache so the row we just sent into reflects the
 *  new lastMessage AND pops to the top of the inbox the same frame the
 *  user hits Send. Without the reorder, the row's createdAt is "now" but
 *  it stays where it was until the 60s poll re-sorts — which feels like
 *  a janky bounce. We lift it out wherever it was and prepend to page 0.
 *
 *  Drives:
 *    • Yellow "read · awaiting your reply" dot → gone (lastFromFan now us).
 *    • Row jumps to the top of the inbox instantly.
 *  Walks all chats queries because Unified Inbox vs per-model produces
 *  different query keys, but the row to patch is the same. The full key is
 *    ["chats", scope.kind, accountKey, filter, listId, query, limit].
 *  We only lift-to-top in the UNFILTERED key (filter==null && listId==null
 *  && query==null) — reordering a filtered/folder/search view would shuffle
 *  rows the user didn't touch, and the lastMessage edit alone can't change
 *  whether a row still satisfies that filter. For filtered caches we only
 *  patch a row ALREADY present (update in place, no reorder), and never
 *  prepend a fresh one. */
function patchChatListAfterSend(
  qc: ReturnType<typeof useQueryClient>,
  accountId: string,
  fanId: FanId,
  msg: { fromUserId: number | string; fromUserName: string; text: string; mediaCount: number; price: number },
): void {
  type Page = { rows: OFChatItem[]; hasMore: boolean };
  type Infinite = { pages: Page[]; pageParams: unknown[] };
  const nowIso = new Date().toISOString();
  const patchRow = (target: OFChatItem): OFChatItem => ({
    ...target,
    hasUnread: false,
    unreadMessagesCount: 0,
    lastMessage: {
      ...(target.lastMessage ?? { id: 0 }),
      id: target.lastMessage?.id ?? 0,
      text: msg.text,
      fromUser: { id: msg.fromUserId, name: msg.fromUserName },
      createdAt: nowIso,
      mediaCount: msg.mediaCount,
      isFree: msg.price === 0,
    },
  });
  qc.getQueryCache().findAll({ queryKey: ["chats"] }).forEach((q) => {
    const data = q.state.data as Infinite | undefined;
    if (!data?.pages || data.pages.length === 0) return;
    const filterKey = q.queryKey[3] ?? null;
    const listIdKey = q.queryKey[4] ?? null;
    const queryKeyText = q.queryKey[5] ?? null;
    const isUnfiltered =
      filterKey === null && listIdKey === null && queryKeyText === null;

    if (!isUnfiltered) {
      // Filtered/folder/search view: patch the row in place if it's already
      // here, otherwise leave it untouched. No reorder, no fresh prepend.
      let found = false;
      const patched: Page[] = data.pages.map((p) => ({
        ...p,
        rows: p.rows.map((c) => {
          if (
            !found &&
            (c.__accountId ?? "") === accountId &&
            c.withUser.id === fanId
          ) {
            found = true;
            return patchRow(c);
          }
          return c;
        }),
      }));
      if (!found) return;
      qc.setQueryData(q.queryKey, { ...data, pages: patched });
      return;
    }

    // Unfiltered key: hunt for the row + lift it out. We rebuild pages
    // without it, then prepend the patched version to page 0. Pages further
    // down lose at most one row each — fine, the next refetch reconciles.
    let target: OFChatItem | null = null;
    const stripped: Page[] = data.pages.map((p) => {
      const kept: OFChatItem[] = [];
      for (const c of p.rows) {
        if (
          target === null &&
          (c.__accountId ?? "") === accountId &&
          c.withUser.id === fanId
        ) {
          target = c;
          continue;
        }
        kept.push(c);
      }
      return { ...p, rows: kept };
    });
    if (!target) return;
    const updated = patchRow(target as OFChatItem);
    const reordered: Page[] = stripped.map((p, i) =>
      i === 0 ? { ...p, rows: [updated, ...p.rows] } : p,
    );
    qc.setQueryData(q.queryKey, { ...data, pages: reordered });
  });
}
