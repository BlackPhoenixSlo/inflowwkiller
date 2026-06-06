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
import { useEmployee } from "@/contexts/EmployeeContext";

import { useChatMessages } from "./useChatMessages";
import { clearPendingScheduled, trackPendingScheduled } from "./usePendingScheduled";

export interface UseSendMessageOpts {
  accountId: string | null;
  fanId: number | null;
  fromUserId?: number;
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
  replyToMessageId?: number;
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

/** Pull OF's `{error:{message:"..."}}` out of the relay's wrapped 502/4xx body.
 *  Returns undefined if the shape doesn't match, so the caller can fall back. */
function extractOfErrorMessage(err: RelayError): string | undefined {
  const body = err.body as { detail?: { upstream_body?: string } } | undefined;
  const raw = body?.detail?.upstream_body;
  if (typeof raw !== "string") return undefined;
  try {
    const parsed = JSON.parse(raw) as { error?: { message?: string } };
    return parsed?.error?.message;
  } catch {
    return undefined;
  }
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

  // Decrementing temp-id counter (negative ids never collide with OF).
  const nextTempIdRef = useRef(-1);
  // Stash the wire body + attached vault metadata per tempId so Retry
  // can replay both the request and re-seed the optimistic bubble.
  const payloadByTempIdRef = useRef<Map<number, { body: SendMessageBody; attached: VaultMedia[] }>>(new Map());
  const [inflight, setInflight] = useState(0);

  const doSend = useCallback(
    async (tempId: number, body: SendMessageBody, capturedAccountId: string, capturedFanId: number) => {
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
      } catch (err) {
        // Leave the optimistic in place but flag it failed so Retry can
        // recover. Spread the upstream details so logs surface OF's
        // actual rejection reason (status + body), not "[object Object]".
        let reason: string | undefined;
        if (err instanceof RelayError) {
          console.warn("[send] failed", { status: err.status, body: err.body, message: err.message });
          // The relay wraps OF's payload as
          //   { detail: { upstream_status, upstream_body: "<json string>" } }
          // Dig the inner OF message out so the UI can show what OF complained about.
          reason = extractOfErrorMessage(err) ?? `HTTP ${err.status}`;
        } else {
          console.warn("[send] failed", err);
          reason = (err as Error)?.message;
        }
        failLocal(tempId, reason);
      } finally {
        setInflight((n) => n - 1);
      }
    },
    [failLocal, reconcileLocal, qc, employeeId],
  );

  // Immediate-send path, extracted so both the no-schedule case and the
  // local-wait short-delay timer can call it without recursion gymnastics.
  const sendNow = useCallback(
    async (input: SendOpts, capturedAccountId: string, capturedFanId: number) => {
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
      patchChatListAfterSend(qc, capturedAccountId, capturedFanId, {
        fromUserId: fromUserId ?? Number(capturedAccountId),
        fromUserName: fromUserName ?? "you",
        text: input.text,
        mediaCount: optimisticMedia.length,
        price: input.price ?? 0,
      });
      await doSend(tempId, body, capturedAccountId, capturedFanId);
    },
    [fromUserId, fromUserName, appendLocal, doSend, qc],
  );

  const send = useCallback(
    async (input: SendOpts) => {
      if (!accountId || fanId == null) {
        console.warn("[send] missing accountId or fanId — refusing to send");
        return;
      }

      // Scheduled path. Two branches:
      //   • Short delay (< 10 min from now): OF's queue lags or rejects
      //     too-near times, so we wait in-page via setTimeout and then
      //     call the normal send path. Caveat: refreshing/closing the
      //     tab loses the pending send. We accept that for short delays.
      //   • Long delay: hand off to OF's queue endpoint so it survives
      //     tab close, can be edited from OF web, etc.
      if (input.scheduledAt) {
        const fireAt = new Date(input.scheduledAt).getTime();
        const delay = fireAt - Date.now();
        if (delay > 0 && delay < 10 * 60 * 1000) {
          const capturedAccountId = accountId;
          const capturedFanId = fanId;
          const tempId = nextTempIdRef.current--;
          const handle = setTimeout(() => {
            clearPendingScheduled(qc, capturedAccountId, capturedFanId, tempId);
            void sendNow(
              { text: input.text, price: input.price, lockedText: input.lockedText, attached: input.attached, previews: input.previews, taggedUsers: input.taggedUsers, giphyId: input.giphyId },
              capturedAccountId,
              capturedFanId,
            );
          }, delay);
          // Park the entry in the per-chat cache so the MessageList can
          // render a "Sending in N min" bubble. Cancel works by clearing
          // the timer + removing this entry.
          trackPendingScheduled(qc, capturedAccountId, capturedFanId, {
            tempId,
            text: input.text,
            fireAt: new Date(fireAt).toISOString(),
            attached: input.attached ?? [],
            price: input.price,
            lockedText: input.lockedText,
            fromUserId,
            fromUserName,
          }, handle);
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
          console.warn("[schedule] failed", err);
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
      await doSend(tempId, body, accountId, fanId);
    },
    [accountId, fanId, fromUserId, fromUserName, hookHandle, doSend, dropLocal],
  );

  const cancelOptimistic = useCallback(
    (tempId: number) => {
      dropLocal(tempId);
      payloadByTempIdRef.current.delete(tempId);
    },
    [dropLocal],
  );

  /** Cancel a local-wait pending-scheduled send: clear the timer +
   *  remove the bubble. No-op if the send already fired. */
  const cancelPendingScheduled = useCallback(
    (tempId: number) => {
      if (!accountId || fanId == null) return;
      clearPendingScheduled(qc, accountId, fanId, tempId);
    },
    [qc, accountId, fanId],
  );

  return { send, retry, cancelOptimistic, cancelPendingScheduled, inflight };
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
  fanId: number,
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
 *  different query keys, but the row to patch is the same. */
function patchChatListAfterSend(
  qc: ReturnType<typeof useQueryClient>,
  accountId: string,
  fanId: number,
  msg: { fromUserId: number; fromUserName: string; text: string; mediaCount: number; price: number },
): void {
  type Page = { rows: OFChatItem[]; hasMore: boolean };
  type Infinite = { pages: Page[]; pageParams: unknown[] };
  const nowIso = new Date().toISOString();
  qc.getQueryCache().findAll({ queryKey: ["chats"] }).forEach((q) => {
    const data = q.state.data as Infinite | undefined;
    if (!data?.pages || data.pages.length === 0) return;
    // Hunt for the row + lift it out. We rebuild pages without it, then
    // prepend the patched version to page 0. Pages further down lose at
    // most one row each — fine, the next refetch reconciles totals.
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
    const updated: OFChatItem = {
      ...(target as OFChatItem),
      hasUnread: false,
      unreadMessagesCount: 0,
      lastMessage: {
        ...((target as OFChatItem).lastMessage ?? { id: 0 }),
        id: (target as OFChatItem).lastMessage?.id ?? 0,
        text: msg.text,
        fromUser: { id: msg.fromUserId, name: msg.fromUserName },
        createdAt: nowIso,
        mediaCount: msg.mediaCount,
        isFree: msg.price === 0,
      },
    };
    const reordered: Page[] = stripped.map((p, i) =>
      i === 0 ? { ...p, rows: [updated, ...p.rows] } : p,
    );
    qc.setQueryData(q.queryKey, { ...data, pages: reordered });
  });
}
