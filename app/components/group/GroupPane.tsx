"use client";

/**
 * GroupPane — one column inside /group. Slim version of ChatSurface
 * tuned for many panes on screen at once:
 *
 *   • Hard 20-message cap (most recent only, no load-older).
 *   • No image/video rendering — media collapses to a "📎 N" chip so a
 *     wall of 8 panes doesn't pull megabytes of OF CDN traffic. PPV /
 *     locked text shows a 🔒 marker.
 *   • Compact composer: textarea + send button. No vault, no schedule,
 *     no quote. PPV price toggle is hidden by default — group mode is
 *     for fast volume chatting, not crafted PPV blasts.
 *
 * Reuses the existing message + send hooks so optimistic bubbles, SSE
 * patches, and retry-on-fail all "just work" without re-implementing.
 */

import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";

import { useAccountLabel } from "@/hooks/useAccounts";
import { useChatMessages } from "@/hooks/useChatMessages";
import { useSendMessage } from "@/hooks/useSendMessage";
import {
  proxyImage,
  relay,
  type OFChatItem,
  type OFMessage,
  type OFUserMini,
} from "@/lib/relay";
import { cn, decodeHtmlEntities, fmtRelTime } from "@/lib/utils";

const MESSAGE_DISPLAY_CAP = 20;

interface MeResp {
  id: number;
  name?: string;
  username?: string;
}

interface UserListResp {
  [id: string]: OFUserMini & {
    avatarThumbs?: { c50?: string; c144?: string };
  };
}

export function GroupPane({
  accountId,
  fanId,
  onClose,
}: {
  accountId: string;
  fanId: number;
  onClose: () => void;
}) {
  const qc = useQueryClient();

  // Reuse the popout's user-lookup query key so cache is shared across
  // routes — if the user just came from a single chat with this fan,
  // the avatar/name renders instantly.
  const userQ = useQuery<OFUserMini | null>({
    queryKey: ["of-user", accountId, fanId],
    queryFn: async () => {
      const resp = await relay.get<UserListResp>(
        `/api/of/v2/users/list?ids=${fanId}&view=m`,
        { accountId },
      );
      const u = resp?.[String(fanId)];
      if (!u) return null;
      return {
        id: fanId,
        name: u.name,
        username: u.username,
        avatar: u.avatarThumbs?.c50 || u.avatarThumbs?.c144 || u.avatar || null,
        customNickname: u.customNickname ?? null,
        displayName: u.displayName ?? null,
        notice: u.notice ?? null,
      };
    },
    enabled: Number.isFinite(fanId),
    staleTime: 5 * 60_000,
    refetchOnWindowFocus: false,
  });

  const meQ = useQuery<MeResp>({
    queryKey: ["of-me", accountId],
    queryFn: () => relay.get<MeResp>("/api/of/v2/users/me", { accountId }),
    staleTime: 60 * 60 * 1000,
    refetchOnWindowFocus: false,
  });

  const handle = useChatMessages({ accountId, fanId, enabled: true });

  // Synthesize the minimal OFChatItem shape useSendMessage expects (it
  // only reads accountId + fanId from props but the chat-list patching
  // helper looks up by withUser.id). Memoize so the sender hook doesn't
  // see a new reference each render.
  const chat: OFChatItem = useMemo(
    () => ({
      withUser: {
        id: fanId,
        name: userQ.data?.name,
        username: userQ.data?.username,
        avatar: userQ.data?.avatar ?? null,
        customNickname: userQ.data?.customNickname ?? null,
      },
      __accountId: accountId,
      hasUnread: false,
    }),
    [accountId, fanId, userQ.data?.name, userQ.data?.username, userQ.data?.avatar, userQ.data?.customNickname],
  );
  void chat;

  const sender = useSendMessage({
    accountId,
    fanId,
    fromUserId: meQ.data?.id,
    fromUserName: meQ.data?.name ?? meQ.data?.username ?? "you",
    hookHandle: handle,
  });

  // Mark-as-read on mount: same fire-and-forget pattern as ChatSurface,
  // without the chat-list cache patch (the group tab doesn't show one).
  const markedRef = useRef(false);
  useEffect(() => {
    if (markedRef.current) return;
    markedRef.current = true;
    relay
      .post(`/api/of/v2/chats/${fanId}/mark-as-read`, undefined, { accountId })
      .catch(() => { /* silent — next ChatSurface open will retry */ });
  }, [accountId, fanId]);

  // Backlog backfill. The SSE handler (useInboxRealtime) eagerly seeds
  // the messages cache with each incoming bubble — which counts as
  // "fresh data" to useQuery, so opening a pane on a chat that's only
  // ever been touched by SSE (never via /messages fetch) ends up
  // showing 1–2 bubbles with no context. One refresh() call after first
  // settle pulls the real backlog (limit=30, single OF call) so the
  // pane has history. Gated by a ref so it never re-fires.
  const backfilledRef = useRef(false);
  useEffect(() => {
    if (backfilledRef.current) return;
    if (handle.isLoading) return;
    const len = handle.data?.length ?? 0;
    if (len === 0) return; // wait for first data (empty chat is fine)
    if (len >= 10) return; // already have plenty of context
    backfilledRef.current = true;
    void handle.refresh();
  }, [handle.isLoading, handle.data, handle.refresh]);

  // Last 20 only — sliced after the merge so optimistic bubbles still
  // appear at the bottom on send.
  const messages = useMemo(() => {
    const all = handle.data ?? [];
    return all.slice(Math.max(0, all.length - MESSAGE_DISPLAY_CAP));
  }, [handle.data]);

  const ownerId = meQ.data?.id ?? null;

  const headerName = decodeHtmlEntities(
    userQ.data?.displayName ||
    userQ.data?.customNickname ||
    userQ.data?.name ||
    userQ.data?.username ||
    `fan ${fanId}`,
  );
  const headerAvatar = proxyImage(userQ.data?.avatar ?? null, accountId);
  const accountLabel = useAccountLabel(accountId);

  // Composer state — local, the parent doesn't care.
  const [text, setText] = useState("");
  const [sending, setSending] = useState(false);
  const taRef = useRef<HTMLTextAreaElement | null>(null);

  // Auto-stick to the bottom whenever a new message lands (incoming SSE,
  // optimistic send, or initial load). Tracking the last message id (and
  // pending flag — bubbles flip pending→sent in place) instead of just
  // the array length, because at the 20-message cap the length stays
  // constant while the tail rolls forward as older items fall off.
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const last = messages[messages.length - 1];
  const lastKey = last ? `${String(last.id)}:${last._pending ? 1 : 0}` : "";
  useLayoutEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
  }, [lastKey, messages.length]);

  const canSend = text.trim().length > 0 && !sending;

  const onSubmit = async () => {
    if (!canSend) return;
    const value = text.trim();
    setSending(true);
    setText("");
    try {
      await sender.send({ text: value });
    } finally {
      setSending(false);
      // Re-focus so the user can keep typing without grabbing the mouse.
      taRef.current?.focus();
    }
  };

  return (
    <div className="flex flex-col min-w-0 min-h-0 h-full bg-bg border border-border rounded-lg overflow-hidden">
      {/* Header — fan avatar + name + close. Click name → opens dedicated
       *  popout in a new tab for the full-fidelity surface. */}
      <header className="flex items-center gap-2 px-2 py-1.5 border-b border-border bg-panel">
        <a
          href={`/chat/${encodeURIComponent(accountId)}/${fanId}`}
          target="_blank"
          rel="noreferrer"
          className="flex items-center gap-2 flex-1 min-w-0 hover:bg-bg-elev-1/50 -mx-1 px-1 py-0.5 rounded-md transition-colors"
          title="Open full chat in new tab"
        >
          <div className="w-7 h-7 rounded-full bg-bg-elev-1 grid place-items-center text-xs overflow-hidden shrink-0">
            {headerAvatar ? (
              <img
                src={headerAvatar}
                alt=""
                loading="lazy"
                decoding="async"
                className="w-full h-full object-cover"
              />
            ) : (
              <span>{headerName.slice(0, 1).toUpperCase()}</span>
            )}
          </div>
          <div className="flex-1 min-w-0">
            <div className="text-xs font-medium truncate">{headerName}</div>
            <div className="text-[10px] text-fg-dim truncate">{accountLabel}</div>
          </div>
        </a>
        <button
          type="button"
          onClick={onClose}
          className="text-fg-dim hover:text-fg text-sm leading-none px-1.5 py-0.5 rounded hover:bg-bg-elev-1"
          title="Remove from group"
          aria-label="Remove from group"
        >
          ×
        </button>
      </header>

      {/* Message list — newest at bottom. A useLayoutEffect above pins
       *  the scroll to the bottom on mount + every new message arrival
       *  (SSE or optimistic). */}
      <div
        ref={scrollRef}
        className="flex-1 min-h-0 overflow-y-auto px-2 py-2 space-y-1 flex flex-col"
      >
        {handle.isLoading && messages.length === 0 && (
          <div className="text-[11px] text-fg-dim text-center py-4">Loading…</div>
        )}
        {handle.isError && (
          <div className="text-[11px] text-err text-center py-4">
            Failed to load: {(handle.error as Error)?.message || "unknown"}
          </div>
        )}
        {!handle.isLoading && !handle.isError && messages.length === 0 && (
          <div className="text-[11px] text-fg-dim text-center py-4">No messages yet.</div>
        )}
        {messages.map((m) => (
          <GroupMessageRow key={String(m.id)} msg={m} ownerId={ownerId} />
        ))}
      </div>

      {/* Composer — bare minimum. Enter to send, Shift+Enter for newline. */}
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void onSubmit();
        }}
        className="border-t border-border bg-panel p-1.5 flex items-end gap-1.5"
      >
        <textarea
          ref={taRef}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              void onSubmit();
            }
          }}
          rows={1}
          placeholder={`Message ${headerName}…`}
          className={cn(
            "flex-1 min-w-0 resize-none bg-bg border border-border rounded px-2 py-1.5",
            // Mobile keeps text-base (≥16px) so iOS Safari doesn't auto-zoom
            // on focus; desktop tightens back to text-xs for the dense grid.
            "text-base md:text-xs",
            "placeholder:text-muted focus:outline-none focus:border-accent",
            "max-h-24",
          )}
        />
        <button
          type="submit"
          disabled={!canSend}
          className={cn(
            "shrink-0 px-2.5 py-1.5 rounded text-[11px] font-medium",
            "bg-accent hover:bg-accent-hover text-white",
            "disabled:opacity-40 disabled:cursor-not-allowed",
          )}
        >
          {sender.inflight > 0 ? "…" : "send"}
        </button>
      </form>
      {/* Hint that this is a fast-chat surface only. Helps explain why
       *  there's no vault picker / image preview / etc. */}
      <div className="px-2 py-0.5 text-[9px] text-fg-dim bg-panel border-t border-border/40 flex items-center justify-between">
        <span>fast-chat · last {MESSAGE_DISPLAY_CAP} · no images</span>
        <span>{messages.length}</span>
      </div>
    </div>
  );
}

/** A single bubble. Same direction-inference as MessageList; media gets
 *  collapsed into a chip. Tooltip exposes the createdAt so the chatter
 *  has a sanity check on staleness. */
function GroupMessageRow({
  msg,
  ownerId,
}: {
  msg: OFMessage;
  ownerId: number | null;
}) {
  const outgoing = ownerId != null && msg.fromUser?.id === ownerId;
  const text = stripHtmlPreview(msg.text || "", 600);
  const mediaCount = msg.mediaCount ?? msg.media?.length ?? 0;
  const isLocked = !msg.isFree && (msg.price ?? 0) > 0;
  const failed = !!msg._failed;
  const pending = !!msg._pending && !failed;

  return (
    <div className={cn("flex w-full", outgoing ? "justify-end" : "justify-start")}>
      <div
        className={cn(
          "max-w-[85%] rounded-md px-2 py-1 text-xs break-words",
          outgoing
            ? "bg-accent/90 text-white"
            : "bg-bg-elev-1 text-fg border border-border/40",
          failed && "border border-err text-err bg-err/10",
          pending && "opacity-60",
        )}
        title={msg.createdAt ? fmtRelTime(msg.createdAt) : undefined}
      >
        {text ? <div className="whitespace-pre-wrap">{text}</div> : null}
        {(mediaCount > 0 || isLocked) && (
          <div
            className={cn(
              "mt-0.5 text-[10px] inline-flex items-center gap-1",
              outgoing ? "text-white/80" : "text-fg-dim",
            )}
          >
            {isLocked && <span title={`PPV $${msg.price}`}>🔒</span>}
            {mediaCount > 0 && (
              <span title={`${mediaCount} media attached`}>📎 {mediaCount}</span>
            )}
          </div>
        )}
        {failed && msg._failedReason && (
          <div className="mt-0.5 text-[10px]">{msg._failedReason}</div>
        )}
      </div>
    </div>
  );
}

/** OF returns message bodies as HTML (anchor tags, images). Group panes
 *  render text-only — strip tags + decode entities for a fast preview.
 *  Lossy by design; the full chat surface (popout link in the header)
 *  is one click away for the fidelity case. */
function stripHtmlPreview(s: string, max: number): string {
  const plain = s
    .replace(/<br\s*\/?>/gi, " ")
    .replace(/<\/p\s*>/gi, " ")
    .replace(/<[^>]+>/g, "")
    .replace(/&nbsp;/g, " ")
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, "\"")
    .replace(/&#39;/g, "'")
    .replace(/\s+/g, " ")
    .trim();
  return plain.length > max ? plain.slice(0, max - 1) + "…" : plain;
}
