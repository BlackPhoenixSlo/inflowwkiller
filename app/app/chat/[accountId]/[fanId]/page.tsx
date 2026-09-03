"use client";

/**
 * /chat/[accountId]/[fanId] — one-fan window. Opened from the inbox via the
 * "↗ open in new tab" button so a chatter can keep two conversations side
 * by side on a wide monitor (or pop one out onto a second display).
 *
 * Renders just the ChatSurface — no chat list, no nav distractions. The
 * chat object is synthesized from a /users/list lookup since this page is
 * reachable directly via URL and can't rely on the inbox cache being
 * populated.
 */

import { useIsRestoring, useQuery, useQueryClient } from "@tanstack/react-query";
import { use, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";

import { ChatSurface } from "@/components/chat/ChatSurface";
import { useAccountLabel } from "@/hooks/useAccounts";
import { useDocumentTitle } from "@/hooks/useDocumentTitle";
import { relay, type OFChatItem, type OFMessage, type OFUserMini } from "@/lib/relay";
import { fanIdFromParam, sameUserId, type FanId } from "@/lib/fanId";

interface UserListResp { [id: string]: OFUserMini & {
  avatarThumbs?: { c50?: string; c144?: string };
} }

export default function ChatPopoutPage({
  params,
}: { params: Promise<{ accountId: string; fanId: string }> }) {
  const { accountId, fanId: fanIdStr } = use(params);
  // Keep the id EXACTLY as the URL spelled it. This was `Number(fanIdStr)`,
  // which silently corrupted every Fansly fan: a snowflake exceeds JS's 2^53,
  // so `Number("951404711209099264")` is 951404711209099300 — a fan id that
  // does not exist. The popout then asked the relay for that ghost, which is
  // why "↗ pop out" landed on an empty thread reading "Couldn't refresh:
  // observe-only" while the inbox row it was opened from worked fine.
  //
  // The inbox never had the bug because it passes `chat.withUser.id` straight
  // through from JSON, and the Fansly shim emits ids as STRINGS for exactly
  // this reason. Parsing here was the one place that re-introduced the loss,
  // so the popout's query keys ("messages"/"of-user"/"fan") also disagreed
  // with the inbox's and split the cache. `fanIdFromParam` keeps OF's small
  // numeric ids as numbers (their keys stay identical) and leaves anything
  // too large for float64 as the exact string.
  const fanId = useMemo(() => fanIdFromParam(fanIdStr), [fanIdStr]);
  const accountLabel = useAccountLabel(accountId);

  // Fetch the fan profile so the header shows a name/avatar instead of
  // "fan 12345". Falls back gracefully if /users/list fails.
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
        // Non-null inside the queryFn: `enabled: fanId != null` gates it.
        id: fanId as FanId,
        name: u.name,
        username: u.username,
        avatar: u.avatarThumbs?.c50 || u.avatarThumbs?.c144 || u.avatar || null,
        customNickname: u.customNickname ?? null,
      };
    },
    enabled: fanId != null,
    staleTime: 5 * 60_000,
    refetchOnWindowFocus: false,
  });

  // Observe-only subscription to the messages cache. ChatSurface's
  // useChatMessages drives the actual fetch; we just want to react to
  // new entries landing (via SSE patches in useInboxRealtime) so we can
  // bump the tab-title unread count. queryFn is a no-op because
  // `enabled: false` prevents it from running, but React Query still
  // requires the function to be defined.
  const messagesQ = useQuery<OFMessage[]>({
    queryKey: ["messages", accountId, fanId],
    queryFn: () => Promise.reject(new Error("observe-only")),
    enabled: false,
  });

  // Track tab visibility. Document is unavailable during SSR — start
  // assuming visible so the first paint doesn't briefly mark hidden.
  const [hidden, setHidden] = useState(false);
  useEffect(() => {
    const apply = () => setHidden(document.visibilityState !== "visible");
    apply();
    document.addEventListener("visibilitychange", apply);
    return () => document.removeEventListener("visibilitychange", apply);
  }, []);

  // Count new from-fan messages that arrived while the tab was hidden.
  // Resets to zero when the tab becomes visible again (the user has seen
  // the chat).
  const lastMaxIdRef = useRef<number>(0);
  const [unreadHidden, setUnreadHidden] = useState(0);
  useEffect(() => {
    const msgs = messagesQ.data ?? [];
    if (msgs.length === 0) return;
    let maxId = 0;
    for (const m of msgs) {
      const id = Number(m.id);
      if (Number.isFinite(id) && id > maxId) maxId = id;
    }
    const prev = lastMaxIdRef.current;
    if (prev === 0) {
      // First snapshot — record baseline, don't bump for already-loaded history.
      lastMaxIdRef.current = maxId;
      return;
    }
    if (maxId <= prev) return;
    // accountId IS the creator's own id — a fromUser.id that is NOT it means
    // the message came from the fan. Compared through sameUserId because the
    // Fansly path sends string snowflakes where OF sends numbers.
    let bump = 0;
    for (const m of msgs) {
      const id = Number(m.id);
      if (!Number.isFinite(id) || id <= prev) continue;
      if (m.fromUser?.id != null && !sameUserId(m.fromUser.id, accountId)) bump++;
    }
    lastMaxIdRef.current = maxId;
    if (bump > 0 && hidden) setUnreadHidden((n) => n + bump);
  }, [messagesQ.data, hidden, accountId]);
  useEffect(() => {
    if (!hidden) setUnreadHidden(0);
  }, [hidden]);

  // One-shot `?refresh=media`: tip/purchase toasts and Buys & tips rail rows
  // append it because the fan just moved money, so this tab's
  // localStorage-rehydrated caches are stale by seconds. Invalidate the
  // THREAD too, not just the drawer set — the persisted ["messages"]
  // snapshot is what paints first, and a money event changes it (PPV flips
  // locked→purchased, ledger tip rows land) — mirroring the header's manual
  // ↻ (ChatSurface.handleRefresh). Then strip the param so a plain reload
  // doesn't re-fire. Must wait out the persister's restore — invalidating
  // mid-restore would let the rehydrated snapshot land on top of the fresh
  // refetch.
  const qc = useQueryClient();
  const isRestoring = useIsRestoring();
  const refreshFiredRef = useRef(false);
  useEffect(() => {
    if (isRestoring || refreshFiredRef.current) return;
    if (fanId == null) return;
    const url = new URL(window.location.href);
    if (url.searchParams.get("refresh") !== "media") return;
    refreshFiredRef.current = true;
    void qc.invalidateQueries({ queryKey: ["messages", accountId, fanId] });
    void qc.invalidateQueries({ queryKey: ["fan-chat-media", accountId, fanId], exact: false });
    void qc.invalidateQueries({ queryKey: ["fan-ppv-history", accountId, fanId], exact: false });
    void qc.invalidateQueries({ queryKey: ["last-purchases", accountId] });
    void qc.invalidateQueries({ queryKey: ["of-user", accountId, fanId] });
    void qc.invalidateQueries({ queryKey: ["fan", accountId, fanId] });
    url.searchParams.delete("refresh");
    const qs = url.searchParams.toString();
    window.history.replaceState(
      window.history.state,
      "",
      url.pathname + (qs ? `?${qs}` : "") + url.hash,
    );
  }, [isRestoring, qc, accountId, fanId]);

  // Synthesize the minimum OFChatItem the surface needs. canSendMessage
  // defaults to true; if OF actually blocks the chat the Composer will
  // surface the 400 reason on first send. Acceptable tradeoff for a popout.
  // Memoize so we don't hand ChatSurface a new object reference on every
  // render — its useMemo/useEffect deps that read off `chat` would otherwise
  // recompute on every parent re-render.
  const chat: OFChatItem = useMemo(() => ({
    withUser: {
      // Only rendered past the `fanId == null` guard below, where this is set.
      id: fanId as FanId,
      name: userQ.data?.name,
      username: userQ.data?.username,
      avatar: userQ.data?.avatar ?? null,
      customNickname: userQ.data?.customNickname ?? null,
    },
    __accountId: accountId,
    hasUnread: false,
  }), [accountId, fanId, userQ.data?.name, userQ.data?.username, userQ.data?.avatar, userQ.data?.customNickname]);

  const label =
    userQ.data?.customNickname
    || userQ.data?.name
    || (userQ.data?.username ? `@${userQ.data.username}` : null)
    || `fan ${fanId}`;
  const tabTitle = unreadHidden > 0
    ? `(${unreadHidden}) ${label} · Fastt`
    : `${label} · Fastt`;
  useDocumentTitle(tabTitle);

  // Guard AFTER every hook: an early return above any hook makes the hook
  // count differ between renders (React #310) if fanId turns valid in place.
  if (fanId == null) {
    return (
      <div className="grid place-items-center h-chat text-sm text-err">
        Invalid fan id: {fanIdStr}
      </div>
    );
  }

  return (
    <div className="h-chat flex flex-col">
        <div className="border-b border-border px-3 py-1 text-[11px] text-fg-dim flex items-center gap-3 bg-bg-elev-1/40">
          <Link href="/inbox" className="hover:text-fg underline underline-offset-2">
            ← Back to inbox
          </Link>
          <span className="opacity-60">popout · {accountLabel} · fan {fanId}</span>
        </div>
        <div className="flex-1 min-h-0">
          <ChatSurface
            accountId={accountId}
            fanId={fanId}
            chat={chat}
            forceDrawerOpen
            forcePinnedPanelOpen
          />
        </div>
      </div>
  );
}
