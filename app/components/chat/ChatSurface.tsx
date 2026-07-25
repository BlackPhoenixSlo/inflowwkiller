"use client";

/**
 * ChatSurface — the right pane: title bar + MessageList + Composer.
 *
 * Owns the per-chat hook (useChatMessages) and the send hook
 * (useSendMessage) — keeping them here means the ChatList re-rendering
 * doesn't unmount the active chat, and switching chats unmounts/remounts
 * this subtree cleanly (we pass a `key` from the parent).
 *
 * Click the header (avatar/name) to open the FanDrawer with the
 * editable profile overlay.
 */

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { cn, decodeHtmlEntities } from "@/lib/utils";
import { openGroupTab } from "@/lib/groupChannel";

import { useChatMessages } from "@/hooks/useChatMessages";
import { mergeSeedIntoMessages, useChatMessagesLocal } from "@/hooks/useChatMessagesLocal";
import { useChatAttribution } from "@/hooks/useChatAttribution";
import { useSendMessage } from "@/hooks/useSendMessage";
import { useLikeMessage } from "@/hooks/useLikeMessage";
import { useTogglePinMessage } from "@/hooks/useTogglePinMessage";
import { useUnsendMessage } from "@/hooks/useUnsendMessage";
import { useFan } from "@/hooks/useFan";
import AiStatusStrip from "@/components/chat/AiStatusStrip";
import { useAccountLabel } from "@/hooks/useAccounts";
import { useRosterCountActions } from "@/hooks/useRosterCounts";
import { readFanDrawerDefault, useFanDrawerDefault } from "@/hooks/useFanDrawerDefault";
import { useTranslateMode } from "@/hooks/useTranslateMode";
import { useTranslateStatus } from "@/hooks/useTranslations";
import {
  useServerScheduledSends,
  scheduledToPseudoMessage,
  useCancelServerScheduled,
} from "@/hooks/useServerScheduledSends";
import { useEmployee } from "@/contexts/EmployeeContext";
import { proxyImage, relay, type OFChatItem, type OFMessage, type OFUserMini } from "@/lib/relay";
import { stripHtmlPreview } from "@/lib/htmlPreview";

import { MessageList } from "./MessageList";
import { Composer, type ComposerApi } from "./Composer";
import { FanDrawer } from "./FanDrawer";
import { ScheduledForChat } from "./ScheduledForChat";
import { ChatSearch } from "./ChatSearch";
import { ChatActionsMenu } from "./ChatActionsMenu";
import { ModelInfoButton } from "./ModelInfoButton";
import { PinnedBar, PinnedPopover, PinnedSidePanel } from "./PinnedPanel";

export interface QuotedReply {
  /** OF message id we're quoting from. */
  messageId: number;
  /** Plain-text preview shown in the composer + prepended on send. */
  preview: string;
  /** Author name for the preview header — defaults to "fan" if absent. */
  authorName: string;
}

interface MeResp {
  id: number;
  name?: string;
  username?: string;
}

/** Per-account localStorage key holding the model's OF user id. There is
 *  NO synchronous OF user-id source today — the Account model has no
 *  of_user_id column and `meQ` (/users/me) is async, null on first paint.
 *  Caching the resolved id under this key lets the NEXT chat-open hydrate
 *  it synchronously on mount, so bubbles start on the correct side instead
 *  of all-left-then-spread once meQ lands. */
const ownerIdCacheKey = (accountId: string) => `of-owner:${accountId}`;

/** Read the cached owner id synchronously (SSR-safe). Returns null when
 *  absent / unparseable so the caller falls back to the async meQ. */
export function readCachedOwnerId(accountId: string): number | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.localStorage.getItem(ownerIdCacheKey(accountId));
    if (!raw) return null;
    const n = Number(raw);
    return Number.isFinite(n) && n > 0 ? n : null;
  } catch {
    return null;
  }
}

export function ChatSurface({
  accountId, fanId, chat, forceDrawerOpen = false, forcePinnedPanelOpen = false, onBack,
}: {
  accountId: string;
  fanId: number;
  chat: OFChatItem;
  /** Force the FanDrawer to be pinned + open regardless of the user's
   *  "keep open by default" setting. Used by the standalone popout
   *  window at /chat/[accountId]/[fanId] where the right-side fan info
   *  is the whole point of opening a dedicated window. */
  forceDrawerOpen?: boolean;
  /** Promote the pinned-messages popover into a full-height left
   *  column. Used by the popout — the inbox-style space on the left
   *  has no ChatList there, so it becomes a permanent pinned-messages
   *  surface mirroring the fan-info panel on the right. Hides the
   *  top bar + bottom-left popover to avoid duplicate surfaces. */
  forcePinnedPanelOpen?: boolean;
  /** Optional mobile-only back arrow. When provided, the header gets a
   *  `<` button on the left (visible below md) that runs this callback.
   *  /inbox uses it to clear the selected chat and reveal the ChatList
   *  again on small screens; popout / group don't pass it. */
  onBack?: () => void;
}) {
  const { current: currentEmployee } = useEmployee();
  const qc = useQueryClient();
  const { refreshOne: refreshRosterOne, patchLocal: patchRosterLocal } =
    useRosterCountActions();

  const [drawerKeepOpenPref] = useFanDrawerDefault();
  const drawerKeepOpen = forceDrawerOpen || drawerKeepOpenPref;
  const [drawerOpen, setDrawerOpen] = useState(
    () => {
      // Below md the persisted "keep open" preference is overruled, and
      // so is forceDrawerOpen: a 360px viewport doesn't have room for
      // chat + drawer, and a deep-link would bury the thread under a
      // full-screen drawer. User can still tap the header to open the
      // overlay drawer manually. Desktop is unaffected — this guard
      // never matches at >=768px, so the checks below run as before.
      if (typeof window !== "undefined"
        && window.matchMedia("(max-width: 767px)").matches) {
        return false;
      }
      // forceDrawerOpen (popout window) wins over everything.
      if (forceDrawerOpen) return true;
      return readFanDrawerDefault();
    },
  );
  const [quoted, setQuoted] = useState<QuotedReply | null>(null);
  const [searchOpen, setSearchOpen] = useState(false);
  // 🌐 header toggle — persisted; MessageList reads the same hook and does
  // the actual translation fetch/render. Status feeds the button's
  // "…" (fetching) / "⚠" (batch failed) indicators.
  const [translateOn, setTranslateOn] = useTranslateMode();
  const translateStatus = useTranslateStatus();
  const [highlightId, setHighlightId] = useState<number | null>(null);
  const [actionsOpen, setActionsOpen] = useState(false);
  const [pinnedOpen, setPinnedOpen] = useState(false);
  // 👁 "Focus" — a phone reading mode. Collapses the AI status strip and the
  // search/translate header controls so the thread gets the full screen on a
  // 360px phone. Persisted globally (a personal reading preference, not
  // per-fan) and only ever toggleable below lg, where the header crowds the
  // conversation — desktop never sees the button so focusMode stays false
  // there and the extra chips render as before.
  const [focusMode, setFocusMode] = useState(false);
  useEffect(() => {
    if (typeof window === "undefined") return;
    setFocusMode(window.localStorage.getItem("chatFocusMode") === "1");
  }, []);
  const toggleFocus = useCallback(() => {
    setFocusMode((v) => {
      const next = !v;
      try {
        window.localStorage.setItem("chatFocusMode", next ? "1" : "0");
      } catch {
        /* private-mode / quota — the toggle still works for this session */
      }
      return next;
    });
  }, []);
  // Imperative handle on Composer — lets FanDrawer's "Send to chat"
  // buttons (on unsold PPVs) preload the composer with that PPV's
  // text + media + price, identical to picking a saved template.
  const composerApiRef = useRef<ComposerApi | null>(null);

  // When the user flips the persisted toggle while a chat is open, mirror
  // the change into local state so the drawer opens/closes live without
  // requiring a chat-switch (which is what triggers the initializer).
  // Same mobile guard as the initializer — never auto-open on small
  // screens; the user must explicitly tap the header.
  useEffect(() => {
    if (typeof window !== "undefined"
      && window.matchMedia("(max-width: 767px)").matches) {
      return;
    }
    setDrawerOpen(drawerKeepOpen);
  }, [drawerKeepOpen]);

  // Resolve the model account's OF user id — needed to decide direction
  // (outgoing if message.fromUser.id === ownerUserId).
  const meQ = useQuery<MeResp>({
    queryKey: ["of-me", accountId],
    queryFn: () => relay.get<MeResp>("/api/of/v2/users/me", { accountId }),
    staleTime: 60 * 60 * 1000,
    refetchOnWindowFocus: false,
  });

  // Owner id, hydrated SYNCHRONOUSLY from localStorage on mount so the very
  // first paint already has it (no all-left-then-spread flash while meQ is
  // in flight). meQ is the source of truth once it lands; until then the
  // cached value from the previous visit stands in. The component is keyed
  // per-account by the parent, so this initializer re-reads when the
  // account changes. ownerUserId below prefers the fresh meQ id.
  const [cachedOwnerId, setCachedOwnerId] = useState<number | null>(
    () => readCachedOwnerId(accountId),
  );
  useEffect(() => {
    const id = meQ.data?.id;
    if (id == null) return;
    setCachedOwnerId(id);
    try {
      window.localStorage.setItem(ownerIdCacheKey(accountId), String(id));
    } catch { /* quota / privacy mode — the async meQ still drives this session */ }
  }, [accountId, meQ.data?.id]);
  const ownerUserId = meQ.data?.id ?? cachedOwnerId;

  const handle = useChatMessages({ accountId, fanId, enabled: true });

  // Cold-start seed: local SQLite via /admin/messages/{aid}/{fid}, mapped
  // to OFMessage shape. Lives in a SEPARATE cache key
  // (["messages-local-seed", ...]) so it never registers a competing
  // queryFn on ["messages", aid, fid] (two useQuery on one key would race
  // their fetches and the real OF call could be skipped). Fetches only
  // while `handle` has no data; once OF (or the patcher) populates the
  // real cache it's disabled and never read for rendering again — the
  // hydration effect below copies it across instead.
  // Always enabled — NOT gated on `handle.isPending`. The `"messages"` cache is
  // persisted to localStorage (see providers.tsx), so on a popout / repeat-open
  // isPending is already false and a pending-gated seed would never run — which
  // is exactly how ledger-synthesized TIP rows (their ONLY render path is this
  // seed) went missing from the thread. It's one cheap indexed sqlite query
  // (5-min staleTime, no refetch-on-mount/focus), and the one-time hydration
  // below only ADDS rows the cache is missing, so the OF fetch / SSE patcher
  // stay authoritative.
  const seedQ = useChatMessagesLocal({
    enabled: true,
    accountId,
    fanId,
  });

  // DB-as-render-source: copy the seed ONE-TIME into the real
  // ["messages", accountId, fanId] cache so DB history renders instantly
  // through the SAME query the realtime patcher (useInboxRealtime) and the
  // send-flow (appendLocal/reconcileLocal) write to. The OF fetch then
  // reconciles into that key in place — seed rows and OF rows share the
  // real `message_id`, so the MessageList keys (key={String(m.id)}) match
  // and the swap is a prop update, not a remount: no mid-session flash.
  //
  // Guards that keep the patcher the SOLE ongoing writer:
  //   • a ref so this fires at most once per chat-open (ChatSurface is
  //     keyed per accountId:fanId, so the ref resets on every switch);
  //   • a functional updater that NEVER clobbers an already-populated
  //     cache — if OF resolved first, or the patcher landed a live message
  //     in the cold-open window, we only fold in seed rows the cache is
  //     missing, leaving the richer existing rows authoritative.
  // Deliberately NOT `initialData` on useChatMessages: that re-applies on
  // every render and fights the patcher.
  const seedHydratedRef = useRef(false);
  useEffect(() => {
    if (seedHydratedRef.current) return;
    if (!accountId || fanId == null) return;
    if (seedQ.rows.length === 0) return;
    seedHydratedRef.current = true;
    qc.setQueryData<OFMessage[]>(
      ["messages", accountId, fanId],
      (prev) => mergeSeedIntoMessages(prev, seedQ.rows),
    );
  }, [seedQ.rows, accountId, fanId, qc]);

  // Prefetch the fan row so opening the drawer is instant. The hook
  // is cheap (one indexed sqlite lookup) and the data drives the
  // custom_nickname we want to show in the header.
  const fanQ = useFan(accountId, fanId);
  const accountLabel = useAccountLabel(accountId);

  // Observe the per-fan profile cache that enrichWithUsers populates from
  // /users/list. ChatList already reads from this side channel so its rows
  // pick up nickname/name/avatar; the surface header needs the same path
  // because the `chat` prop is the slim row (enrichment writes to this
  // cache, not back into the chats list cache). queryFn is a placeholder
  // that never runs (enabled:false) but satisfies react-query v5's
  // "missing queryFn" warning — the cache is written by enrichWithUsers
  // + useFan's onSuccess, not by this query.
  const profileQ = useQuery<OFUserMini>({
    queryKey: ["of-user", accountId, fanId],
    queryFn: () => Promise.reject(new Error("observe-only")),
    enabled: false,
    staleTime: Infinity,
  });
  const profile = profileQ.data;

  // Header ↻ refresh: a full re-pull of this chat AND the fan, plus a
  // re-cache into the local SQLite mirror. The plain `handle.refresh()`
  // only re-fetched the message page, so a fan who just paid a tip / a PPV
  // could keep showing the stale "unseen"/locked state and the drawer's
  // sales/revenue lagged. This invalidates every cache the chat + fan panel
  // render from, then asks the server to re-scrape this fan's chat from OF
  // so anything the live pull alone misses lands in the persisted history.
  const [isRefreshing, setIsRefreshing] = useState(false);
  const handleRefresh = useCallback(async () => {
    if (!accountId || fanId == null) return;
    setIsRefreshing(true);
    try {
      await Promise.all([
        // Live OF re-pull — messages, fan profile (nickname/notice/spend),
        // local fan row, the PPV gallery + ledger spine behind the drawer's
        // chart/Sales, and the roster unread badges.
        qc.invalidateQueries({ queryKey: ["messages", accountId, fanId] }),
        qc.invalidateQueries({ queryKey: ["of-user", accountId, fanId] }),
        qc.invalidateQueries({ queryKey: ["fan", accountId, fanId] }),
        qc.invalidateQueries({
          queryKey: ["fan-chat-media", accountId, fanId],
          exact: false,
        }),
        qc.invalidateQueries({
          queryKey: ["fan-ppv-history", accountId, fanId],
          exact: false,
        }),
        qc.invalidateQueries({ queryKey: ["last-purchases", accountId] }),
        // Force this model's badge authoritative (busts its server cache + re-reads
        // only it); a plain invalidate would just refetch the stale cached count.
        refreshRosterOne(accountId, { force: true }),
        // #11: also refresh the account's list/folder membership view so Refresh
        // updates which lists the fan is in / can be added to — the folder chips
        // and counts — not just this fan's messages/profile. (of-user above
        // already refreshes the per-fan listsStates.)
        qc.invalidateQueries({ queryKey: ["chat-folders", accountId] }),
        // Re-cache into the local mirror: re-scrape this fan's chat from OF so
        // the persisted history catches a just-paid PPV/tip that the live UI
        // pull alone wouldn't reconcile. Runs INLINE (not the automation queue)
        // so it lands in ~1-2s even while a big background scrape sweep is
        // hogging the bulk lane — the queued path would wait behind it.
        // Best-effort — the UI already refreshed from OF above regardless.
        relay
          .post(
            `/admin/messages/${accountId}/${fanId}/rescrape-now`,
            undefined,
            { accountId },
          )
          .catch(() => { /* re-cache is best-effort; UI is already fresh */ }),
      ]);
    } finally {
      setIsRefreshing(false);
    }
  }, [qc, accountId, fanId, refreshRosterOne]);

  // Mark the chat read on open. Patches the inbox cache locally so the
  // blue dot disappears immediately, then fires the server POST in the
  // background (fire-and-forget — OF will catch up on its next poll
  // even if our request 5xxs).
  useEffect(() => {
    let cancelled = false;
    // (1) LOCAL read-marker — zero our chats.unread_count regardless of OF's
    //     state. Opening a chat we DON'T reply to must move it off the roster's
    //     blue (unread) count and onto orange (owe reply). Fires even when OF
    //     already considered it read (chat.hasUnread === false), so it can't be
    //     gated on hasUnread. Refresh the roster badge only when it cleared.
    relay
      .post<{ cleared?: boolean }>(`/admin/messages/${accountId}/${fanId}/mark-read`, undefined)
      .then((res) => {
        if (res?.cleared && !cancelled) {
          // Opening an unread chat moves it off the roster's BLUE (unread) and
          // onto ORANGE (read, fan spoke last) in the same tick — patch the cached
          // badge optimistically so it drops instantly. We deliberately DON'T
          // re-read OF here: the mark-read POST already busts the server-side
          // roster cache, and the badge is OF-truth, so an immediate re-read could
          // race OF's own read-processing and flicker the count back up. The 60s
          // poll (now reading a busted cache) reconciles to OF-truth cleanly.
          patchRosterLocal(accountId, (c) => ({
            unread: Math.max(0, c.unread - 1),
            owe_reply: c.owe_reply + 1,
          }));
        }
      })
      .catch((err) => console.warn("[mark-read local] failed", err));
    // (2) OF read-marker — only when OF still shows it unread (avoids a needless
    //     upstream call + "seen" receipt on an already-read chat).
    if (chat.hasUnread) {
      relay
        .post(`/api/of/v2/chats/${fanId}/mark-as-read`, undefined, { accountId })
        .catch((err) => console.warn("[mark-as-read] failed", err));
    }
    type Page = { rows: OFChatItem[]; hasMore: boolean };
    type Infinite = { pages: Page[]; pageParams: unknown[] };
    qc.getQueryCache().findAll({ queryKey: ["chats"] }).forEach((q) => {
      const data = q.state.data as Infinite | undefined;
      if (!data?.pages) return;
      const newPages: Page[] = data.pages.map((p) => ({
        ...p,
        rows: p.rows.map((c) =>
          (c.__accountId ?? "") === accountId && c.withUser.id === fanId
            ? { ...c, hasUnread: false, unreadMessagesCount: 0 }
            : c,
        ),
      }));
      if (!cancelled) qc.setQueryData(q.queryKey, { ...data, pages: newPages });
    });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [accountId, fanId, patchRosterLocal]);

  const sender = useSendMessage({
    accountId,
    fanId,
    fromUserId: meQ.data?.id,
    fromUserName: meQ.data?.name ?? meQ.data?.username ?? "you",
    hookHandle: handle,
  });
  const liker = useLikeMessage(accountId, fanId);
  const pinner = useTogglePinMessage(accountId, fanId);
  const unsender = useUnsendMessage(accountId, fanId);

  // Track which mass-send queue ids we've already offered "unsend from
  // everyone" for in this session — if the user unsends multiple bubbles
  // belonging to the same broadcast, only one prompt fires.
  const promptedQueuesRef = useRef<Set<number>>(new Set());

  const handleUnsend = useCallback(async (msg: OFMessage) => {
    const id = Number(msg.id);
    if (!Number.isFinite(id) || id <= 0) return;
    // First prompt: confirm the per-chat unsend.
    if (!window.confirm("Unsend this message from this chat? OF allows this within ~24h.")) return;
    let resp: Awaited<ReturnType<typeof unsender.unsendOne>> = null;
    try {
      resp = await unsender.unsendOne(msg);
    } catch (err) {
      const reason = err instanceof Error ? err.message : String(err);
      window.alert(`Couldn't unsend: ${reason}`);
      return;
    }
    // If OF echoed a `queue` block, this was a mass message. Offer the
    // "also unsend from everyone" follow-up, but only if OF still says
    // the queue is unsendable (canUnsend=true and not already canceled).
    const queue = resp?.queue;
    if (!queue?.id) return;
    if (promptedQueuesRef.current.has(queue.id)) return;
    promptedQueuesRef.current.add(queue.id);
    if (queue.isCanceled || queue.canUnsend === false) return;
    const total = queue.sentCount ?? 0;
    const viewed = queue.viewedCount ?? 0;
    const summary = total
      ? `Sent to ${total} fan${total === 1 ? "" : "s"}${viewed ? `, ${viewed} viewed` : ""}.`
      : "";
    const ok = window.confirm(
      `This message is part of a mass send. ${summary} Also unsend from EVERYONE who received it?`,
    );
    if (!ok) return;
    try {
      await unsender.unsendQueue(queue.id);
    } catch (err) {
      const reason = err instanceof Error ? err.message : String(err);
      window.alert(`Mass-unsend failed: ${reason}`);
    }
  }, [unsender]);

  // Manual "🎁 Send reward" on a tip bubble → fire the tip_reward automation
  // for this fan on demand (force=true bypasses the once-per-tip guard so a
  // chatter can re-reward). The endpoint runs the automation inline and returns
  // its real result, so we can confirm exactly what was sent. Confirms first —
  // it sends real vault images, like the unsend flow above.
  const handleSendReward = useCallback(async (msg: OFMessage) => {
    if (!accountId || fanId == null) return;
    const dollars = typeof msg.price === "number" ? msg.price : 0;
    const amountLabel = dollars > 0 ? ` ($${dollars.toFixed(2)} tip)` : "";
    if (!window.confirm(
      `Send a free vault reward to this fan${amountLabel}? This fires the tip_reward automation and sends real images.`,
    )) return;
    const messageId = Number(msg.id);
    try {
      const resp = await relay.post<{
        ok?: boolean; status?: string; reason?: string; images_sent?: number;
      }>("/admin/tip-reward/send", {
        account_id: accountId,
        fan_id: fanId,
        tip_cents: Math.round(dollars * 100) || undefined,
        tip_message_id: Number.isFinite(messageId) && messageId > 0 ? messageId : undefined,
      }, { accountId });
      const sent = resp?.images_sent ?? 0;
      if ((resp?.ok ?? resp?.status === "ok") && sent > 0) {
        window.alert(`🎁 Sent ${sent} reward image${sent === 1 ? "" : "s"}.`);
      } else if (resp?.status === "ok") {
        window.alert(
          "No reward sent — this fan already received every image in the matching tier (or no tier folder is configured).",
        );
      } else {
        window.alert(`Couldn't send reward: ${resp?.reason ?? resp?.status ?? "unknown error"}`);
      }
    } catch (err) {
      const reason = err instanceof Error ? err.message : String(err);
      window.alert(`Couldn't send reward: ${reason}`);
    }
  }, [accountId, fanId]);

  // Pre-warm only the fan-specific cache here — the account-wide ones
  // (vault-lists, wall-media) are kicked off from the inbox page at app
  // start so they don't compete with the message fetch on chat-open.
  // Delay even this small query so the visible message list gets first
  // crack at the relay's request budget.
  useEffect(() => {
    if (!accountId || fanId == null) return;
    const t = setTimeout(() => {
      qc.prefetchQuery({
        queryKey: ["vault-history", accountId, fanId],
        queryFn: () =>
          relay.get(`/admin/vault/fan-history?account_id=${encodeURIComponent(accountId)}&fan_id=${fanId}`),
        staleTime: 60_000,
      }).catch(() => {});
    }, 500);
    return () => clearTimeout(t);
  }, [accountId, fanId, qc]);

  // Backfill historical vault-sends from chat messages already loaded.
  // We hooked the auto-record at send-time AFTER feature-ship, so any
  // chat that existed before that has no rows — meaning the picker's
  // sent/purchased rings stay empty. Walking the message stream and
  // batch-POSTing missing rows fills that gap lazily, scoped to the
  // chat the user is actually looking at.
  //
  // Idempotent on the backend (dupe-checks by message_id) so it's safe
  // to fire on every load — older pages picked up via loadOlder also
  // trigger this when handle.data changes.
  const backfilledMsgIdsRef = useRef<Set<number>>(new Set());
  useEffect(() => {
    if (!accountId || fanId == null) return;
    const messages = handle.data ?? [];
    const myId = meQ.data?.id;
    if (!myId || messages.length === 0) return;

    const items: Array<{
      message_id: number;
      media_ids: number[];
      price_cents: number;
      was_purchased: boolean | null;
    }> = [];
    for (const m of messages) {
      const mid = typeof m.id === "number" ? m.id : Number(m.id);
      if (!Number.isFinite(mid) || mid <= 0) continue;
      // Only outgoing messages count — incoming ones come from the fan,
      // not from us, so they have no business in vault_sends.
      if (m.fromUser?.id !== myId) continue;
      const mediaIds = (m.media ?? [])
        .map((x) => (typeof x.id === "number" ? x.id : Number(x.id)))
        .filter((n) => Number.isFinite(n) && n > 0);
      if (mediaIds.length === 0) continue;
      if (backfilledMsgIdsRef.current.has(mid)) continue;
      items.push({
        message_id: mid,
        media_ids: mediaIds,
        price_cents: Math.max(0, Math.round((m.price || 0) * 100)),
        // PPV: isOpened flips true once the fan paid. For free sends OF
        // also reports isOpened=true when the fan opens the chat, which
        // is meaningless — leave was_purchased null in that case.
        was_purchased: (m.price ?? 0) > 0 ? !!m.isOpened : null,
      });
      backfilledMsgIdsRef.current.add(mid);
    }
    if (items.length === 0) return;

    relay
      .post("/admin/vault/sends/backfill", {
        account_id: accountId,
        fan_id: fanId,
        items,
      })
      .then((r) => {
        const resp = r as { inserted?: number } | undefined;
        if ((resp?.inserted ?? 0) > 0) {
          qc.invalidateQueries({ queryKey: ["vault-history", accountId, fanId] });
        }
      })
      .catch((err) => console.warn("[vault backfill] failed", err));
  }, [accountId, fanId, handle.data, meQ.data?.id, qc]);

  // #19: the vault "restock" — the SENT / PURCHASED / UNSEEN badges the picker
  // reads from ["vault-history"] — must refresh every time a chat is opened,
  // from ANY entry point (a Tips-tab row, a PPV-message row, a deep link). The
  // backfill effect above only invalidates when it *inserts* new rows, so a chat
  // whose sends were already backfilled on an earlier open (e.g. first reached
  // from Chat messages) never re-fetched its history when later opened from a
  // tip — the reported "restock doesn't trigger from a tip" bug. A plain
  // invalidate on open makes the restock entry-point-agnostic; it's a cheap,
  // idempotent read (a no-op while the picker isn't mounted).
  useEffect(() => {
    if (!accountId || fanId == null) return;
    qc.invalidateQueries({ queryKey: ["vault-history", accountId, fanId] });
  }, [accountId, fanId, qc]);

  // The `chat` prop is a snapshot captured at click time and never refreshed.
  // After a scope switch the ChatList serves placeholderData while the new
  // scope's /chats call is in flight, so the row the user clicks can carry
  // a stale `canSendMessage: false` from the previous scope. That stale
  // false latches the Composer's "Sending disabled" banner + disables Send
  // until the user re-mounts via a sibling chat click. Resolve gating
  // fields from the live chats cache instead, with a subscription that
  // re-renders when any ["chats", ...] entry updates so polling/SSE feeds
  // through here too.
  const [chatsCacheTick, setChatsCacheTick] = useState(0);
  useEffect(() => {
    const unsubscribe = qc.getQueryCache().subscribe((event) => {
      // Only react to the events that change a cached chat row's fields.
      // `observerResultsUpdated` fires on every dependent component render
      // and would loop us into useless re-renders.
      if (event.type !== "added" && event.type !== "updated") return;
      const key = event.query.queryKey;
      if (Array.isArray(key) && key[0] === "chats") {
        // Defer the setState past the current commit. React Query fires
        // `observerAdded` synchronously while a consumer component (e.g.
        // ChatList) is rendering — calling setState on ChatSurface from
        // inside that callback trips React's "setState while rendering
        // another component" warning. queueMicrotask lands the update
        // after the in-flight render finishes.
        queueMicrotask(() => setChatsCacheTick((t) => t + 1));
      }
    });
    return unsubscribe;
  }, [qc]);
  const liveChat = useMemo<OFChatItem | null>(() => {
    type Page = { rows: OFChatItem[]; hasMore: boolean };
    type Infinite = { pages: Page[]; pageParams: unknown[] };
    for (const q of qc.getQueryCache().findAll({ queryKey: ["chats"] })) {
      const data = q.state.data as Infinite | undefined;
      if (!data?.pages) continue;
      for (const p of data.pages) {
        for (const c of p.rows) {
          if ((c.__accountId ?? "") === accountId && c.withUser.id === fanId) {
            return c;
          }
        }
      }
    }
    return null;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [accountId, fanId, qc, chatsCacheTick]);

  // The chat-list row holds OF's `lastReadMessageId` — the highest msg id
  // the fan has marked read. We prefer the prop (live from inbox), but
  // fall back to the chat-list cache so the popout page can also show ✓✓.
  const lastReadByPeerId = useMemo<number | null>(() => {
    if (chat.lastReadMessageId != null) return chat.lastReadMessageId;
    if (liveChat?.lastReadMessageId != null) return liveChat.lastReadMessageId;
    return null;
  }, [chat.lastReadMessageId, liveChat]);

  function onQuoteReply(msg: OFMessage) {
    const preview = stripHtmlPreview(msg.text || "(media)", 140);
    setQuoted({
      messageId: Number(msg.id),
      preview,
      authorName: msg.fromUser?.name || msg.fromUser?.username || "fan",
    });
  }

  function jumpToMessage(id: number) {
    setHighlightId(id);
    setSearchOpen(false);
    // Drop the highlight ring after a couple seconds so it doesn't linger
    // through the next scroll-up that brings it back into view.
    setTimeout(() => setHighlightId((cur) => (cur === id ? null : cur)), 3000);
  }

  // Server-scheduled sends live in a separate query; merge them into
  // the message stream as pseudo-bubbles so the operator sees what's
  // about to go out (and can cancel) without leaving the chat. Memoize
  // so the MessageList's "did the array change" effect doesn't fire
  // every render — the auto-scroll-to-bottom depends on a stable ref.
  const scheduledQ = useServerScheduledSends(accountId, fanId);
  const cancelScheduled = useCancelServerScheduled();
  // The message stream always reads the real ["messages", ...] cache: DB
  // history is hydrated into it on open (seed-hydration effect above), the
  // OF fetch reconciles into it, and the realtime patcher + send-flow keep
  // it live. The seed is never read directly here — reading a different
  // array reference is what made the old seed-then-discard swap flash
  // mid-session. Server-scheduled sends (future-dated) are appended as
  // pseudo-rows so they sort to the bottom as "about to go out" ghosts.
  const mergedMessages: OFMessage[] = useMemo(() => {
    const real = handle.data ?? [];
    const scheduled = scheduledQ.data ?? [];
    if (scheduled.length === 0) return real;
    return [...real, ...scheduled.map((s) => scheduledToPseudoMessage(s, ownerUserId))];
  }, [handle.data, scheduledQ.data, ownerUserId]);

  // Pinned slice sourced from the loaded messages cache — no extra OF
  // fetch. Only pinned items inside the currently-loaded backlog show;
  // scrolling up to load older pages will surface older pins too.
  const pinnedMessages = useMemo(
    () => (handle.data ?? []).filter((m) => m.isPinned),
    [handle.data],
  );

  // Outbound-bubble "Sent by {employee}" label data. Anchored at the
  // oldest visible message so loading older pages widens the window
  // without thrashing every render — react-query treats the new
  // oldestId as a separate key and serves the previous data while the
  // wider fetch resolves.
  const oldestVisibleId = useMemo(() => {
    const first = mergedMessages[0]?.id;
    if (first == null) return null;
    const n = typeof first === "number" ? first : Number(first);
    return Number.isFinite(n) && n > 0 ? n : null;
  }, [mergedMessages]);
  const attributionQ = useChatAttribution(accountId, fanId, oldestVisibleId);
  const attributionMap = attributionQ.data?.by_msg_id ?? null;

  // Cross-chatter freshness: when a teammate (Slovenia/Kenya/Nigeria/…)
  // sends from another machine, the WS pump lands the outbound row in
  // useChatMessages but our attribution map is still cached. Rather than
  // shortening staleTime (which would re-fetch even on idle chats), we
  // piggyback on the chat-message arrival itself — invalidate ONLY when
  // a new outbound id appears that isn't in the map. Idle chats pay
  // nothing; active chats pay one fetch per teammate-send observed.
  // (ownerUserId is hydrated synchronously above.)
  useEffect(() => {
    if (!accountId || fanId == null || ownerUserId == null) return;
    if (!attributionMap) return;
    let needsRefresh = false;
    for (const m of mergedMessages) {
      if (m.fromUser?.id !== ownerUserId) continue;
      if ((m._tempId ?? 0) < 0) continue;
      const idNum = typeof m.id === "number" ? m.id : Number(m.id);
      if (!Number.isFinite(idNum) || idNum <= 0) continue;
      if (!(String(idNum) in attributionMap)) { needsRefresh = true; break; }
    }
    if (needsRefresh) {
      qc.invalidateQueries({ queryKey: ["msg-attribution", accountId, fanId] });
    }
  }, [accountId, fanId, ownerUserId, mergedMessages, attributionMap, qc]);

  const headerName = decodeHtmlEntities(
    profile?.displayName ||
    chat.withUser.displayName ||
    fanQ.data?.custom_nickname ||
    profile?.customNickname ||
    chat.withUser.customNickname ||
    profile?.name ||
    chat.withUser.name ||
    profile?.username ||
    chat.withUser.username ||
    `fan ${chat.withUser.id}`,
  );
  const headerAvatarRaw =
    chat.withUser.avatar || profile?.avatar || fanQ.data?.avatar_url || null;
  const headerAvatar = proxyImage(headerAvatarRaw, accountId);

  return (
    <div className="relative flex h-full min-h-0 min-w-0 bg-bg overflow-hidden">
      {forcePinnedPanelOpen && (
        <PinnedSidePanel
          pinned={pinnedMessages}
          ownerUserId={ownerUserId}
          onJumpTo={jumpToMessage}
        />
      )}
      <div className="relative flex flex-col flex-1 min-w-0 min-h-0">
      <header className="relative border-b border-border px-3 md:px-4 py-3 flex items-center gap-2 md:gap-3 bg-panel flex-wrap md:flex-nowrap">
        {onBack && (
          <button
            type="button"
            onClick={onBack}
            className="lg:hidden w-9 h-9 -ml-1 grid place-items-center rounded-lg text-fg-dim hover:text-fg hover:bg-bg-elev-1 shrink-0"
            title="Back to chat list"
            aria-label="Back"
          >
            <span aria-hidden className="text-lg leading-none">‹</span>
          </button>
        )}
        <button
          type="button"
          onClick={() => setDrawerOpen(true)}
          className="flex items-center gap-3 flex-1 min-w-0 text-left hover:bg-bg-elev-1/40 -mx-1 px-1 py-1 rounded-md transition-colors"
          title="Open fan profile"
        >
          <div className="w-9 h-9 rounded-full bg-bg-elev-1 grid place-items-center text-sm overflow-hidden shrink-0">
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
            <div className="font-medium text-sm truncate">{headerName}</div>
            <div className="text-[11px] text-fg-dim truncate">
              @{chat.withUser.username || profile?.username || fanQ.data?.of_username || chat.withUser.id} · {accountLabel}
            </div>
            {/* Why the AI is (or isn't) talking to this fan — see AiStatusStrip.
                Focus mode hides it below lg; desktop (>=lg) always shows it. */}
            <div className={cn(focusMode && "hidden lg:block")}>
              <AiStatusStrip accountId={accountId} fanId={fanId} />
            </div>
          </div>
        </button>
        {/* Stays mounted on phone: it returns null when nothing is queued,
            so it costs no header space in the common case, and it is the
            only way to see/cancel a fan's queued sends. No wrapper div —
            that would add a stray flex gap to the desktop header. */}
        <ScheduledForChat accountId={accountId} fanId={fanId} />
        {/* 👁 Focus — phone-only (lg:hidden). Strips the header down to the
            fan + composer so the thread fills a small screen. Persisted. */}
        <button
          type="button"
          onClick={toggleFocus}
          className={cn(
            "lg:hidden text-[11px] shrink-0",
            focusMode
              ? "px-1.5 py-0.5 rounded-md bg-accent/15 text-accent border border-accent/40 font-medium"
              : "text-fg-dim hover:text-fg underline underline-offset-2",
          )}
          title={focusMode
            ? "Focus mode ON — AI status & header tools hidden for more thread space. Tap to turn off."
            : "Focus mode — hide the AI status strip and header tools to give the conversation the full screen"}
          aria-pressed={focusMode}
        >
          👁 focus{focusMode ? " ON" : ""}
        </button>
        <button
          type="button"
          onClick={() => setSearchOpen((v) => !v)}
          className={cn(
            "text-[11px] underline underline-offset-2",
            searchOpen ? "text-accent" : "text-fg-dim hover:text-fg",
            focusMode && "hidden lg:inline",
          )}
          title="Search this conversation"
        >
          🔍 search
        </button>
        <button
          type="button"
          onClick={() => setTranslateOn(!translateOn)}
          className={cn(
            "text-[11px]",
            translateOn
              ? "px-1.5 py-0.5 rounded-md bg-accent/15 text-accent border border-accent/40 font-medium"
              : "text-fg-dim hover:text-fg underline underline-offset-2",
            focusMode && "hidden lg:inline",
          )}
          title={translateOn
            ? "Translation ON — non-English messages show in English with a (lang) tag; original on hover. Click to turn off."
            : "Translate non-English messages to English — tags each with its detected language, original on hover"}
        >
          🌐 translate{translateOn ? " ON" : ""}
          {translateOn && translateStatus.busy && <span className="animate-pulse"> …</span>}
          {translateOn && !translateStatus.busy && translateStatus.failed && (
            <span title="Last translation batch failed — showing originals"> ⚠</span>
          )}
        </button>
        <a
          href={`/chat/${encodeURIComponent(accountId)}/${fanId}`}
          target="_blank"
          // No rel="noreferrer" / "noopener" on purpose: we want the
          // popout to keep its window.opener set so it can later call
          // window.close() on itself when the user adds it to the
          // group tab via the 👥 button. Same-origin internal link,
          // not security-sensitive.
          className="hidden md:inline text-[11px] text-fg-dim hover:text-fg underline underline-offset-2"
          title="Open this chat in a new tab"
        >
          ↗ pop out
        </a>
        <GroupChatButton accountId={accountId} fanId={fanId} />
        <button
          type="button"
          onClick={() => handleRefresh()}
          title="Refresh chat, fan info & re-cache"
          className="hidden md:inline-flex items-center gap-1 text-[11px] text-fg-dim hover:text-fg underline underline-offset-2"
        >
          <span className={cn("inline-block no-underline", (isRefreshing || handle.isFetching) && "animate-spin")}>↻</span>
          <span>refresh</span>
        </button>
        <div className="hidden md:block">
          <ModelInfoButton accountId={accountId} />
        </div>
        <button
          type="button"
          onClick={() => setActionsOpen((v) => !v)}
          className="text-fg-dim hover:text-fg text-lg leading-none w-10 h-10 grid place-items-center shrink-0 md:block md:w-auto md:h-auto md:px-1 md:shrink"
          title="More actions"
          aria-label="More actions"
        >
          ⋯
        </button>
        {actionsOpen && (
          <ChatActionsMenu
            accountId={accountId}
            fanId={fanId}
            chat={chat}
            onClosed={() => setActionsOpen(false)}
          />
        )}
      </header>

      {searchOpen && (
        <ChatSearch
          accountId={accountId}
          fanId={fanId}
          messages={mergedMessages}
          ownerUserId={ownerUserId}
          hasOlder={handle.hasOlder}
          loadingOlder={handle.isLoadingOlder}
          onLoadOlder={() => handle.loadOlder()}
          onPick={jumpToMessage}
          onClose={() => setSearchOpen(false)}
        />
      )}

      {/* PinnedBar is hidden below md — it eats a row of vertical space
       *  and the popover overlays don't fit comfortably on a 360px screen.
       *  Pinned messages are still visible inline (📌 badges on bubbles)
       *  and the desktop bar is unchanged. */}
      {!forcePinnedPanelOpen && (
        <div className="hidden md:block">
          <PinnedBar
            pinned={pinnedMessages}
            open={pinnedOpen}
            onToggle={() => setPinnedOpen((v) => !v)}
          />
        </div>
      )}

      <MessageList
        messages={mergedMessages}
        ownerUserId={ownerUserId}
        accountId={accountId}
        fanId={fanId}
        isLoading={handle.isLoading}
        isError={handle.isError}
        error={handle.error as Error | null}
        hasOlder={handle.hasOlder}
        loadingOlder={handle.isLoadingOlder}
        onLoadOlder={() => handle.loadOlder()}
        onRetry={sender.retry}
        onCancelScheduled={(jobId) => {
          if (accountId && fanId != null) {
            cancelScheduled.mutate({ jobId, accountId, fanId });
          }
        }}
        onToggleLike={(msg) => liker.toggle(msg)}
        onQuoteReply={onQuoteReply}
        onTogglePin={(msg) => pinner.toggle(msg)}
        onUnsend={handleUnsend}
        onSendReward={handleSendReward}
        highlightId={highlightId}
        lastReadByPeerId={lastReadByPeerId}
        attribution={attributionMap}
        currentEmployeeName={currentEmployee?.display_name ?? null}
      />

      {!forcePinnedPanelOpen && (
        <PinnedPopover
          pinned={pinnedMessages}
          ownerUserId={ownerUserId}
          open={pinnedOpen}
          onClose={() => setPinnedOpen(false)}
          onJumpTo={jumpToMessage}
        />
      )}

      <Composer
        accountId={accountId}
        fanId={fanId}
        composerApiRef={composerApiRef}
        quoted={quoted}
        onClearQuoted={() => setQuoted(null)}
        onSend={(args) => {
          // Closing the pinned popover on send is the second half of the
          // hide rule (the first half is click-out, owned by the popover
          // itself). Keep this before the OF call so the panel disappears
          // even if send returns an error.
          setPinnedOpen(false);
          // OF supports native quote-reply via `replyToMessageId`. Threading
          // it through the send body makes the fan's OF client render the
          // quoted message as a card above the reply — same UX as OF web.
          const replyMsgId = quoted?.messageId;
          // Capture the original message body for the optimistic bubble so
          // we render the quote card immediately, without waiting for OF
          // to echo `replyToMessage` back on the next refetch.
          const replySnapshot = quoted
            ? (() => {
                const orig = (handle.data ?? []).find(
                  (m) => Number(m.id) === quoted.messageId,
                );
                if (!orig) {
                  return {
                    id: quoted.messageId,
                    text: quoted.preview,
                    fromUser: { id: -1, name: quoted.authorName },
                  };
                }
                return {
                  id: Number(orig.id),
                  text: orig.text,
                  fromUser: orig.fromUser,
                  createdAt: orig.createdAt,
                  mediaCount: orig.mediaCount,
                };
              })()
            : null;
          setQuoted(null);
          return sender.send({
            text: args.text,
            price: args.price,
            lockedText: args.lockedText,
            attached: args.attached,
            previews: args.previews,
            scheduledAt: args.scheduledAt,
            replyToMessageId: replyMsgId,
            replyToMessage: replySnapshot,
            taggedUsers: args.taggedUsers,
            giphyId: args.giphyId,
          });
        }}
        inflight={sender.inflight}
        placeholder={`Message ${headerName}…`}
        canSend={
          // Trust the live chat cache; never the prop snapshot. A stale
          // `canSendMessage: false` from a pre-scope-switch click was
          // freezing the composer until the user clicked away and back.
          liveChat ? liveChat.canSendMessage !== false : true
        }
        cannotSendReason={liveChat?.canNotSendReason ?? null}
      />

      {/* Overlay drawer. Renders whenever the drawer is open — on mobile
       *  it's the only branch (the pinned column wouldn't fit at 360px).
       *  On desktop with keep-open ON, the overlay is hidden via
       *  `overlayHideOnDesktopWhenPinned` so the in-flow pinned column
       *  below takes over. */}
      {drawerOpen && (
        <FanDrawer
          open={drawerOpen}
          onClose={() => setDrawerOpen(false)}
          accountId={accountId}
          fanId={fanId}
          chat={chat}
          overlayHideOnDesktopWhenPinned={drawerKeepOpen}
          onComposeWith={(t) => composerApiRef.current?.applyTemplate(t)}
        />
      )}
      </div>
      {/* Pinned drawer — in-flow side column when keep-open is ON. Acts as
       *  a third column inside the inbox (list | chat | drawer). When the
       *  popout passes forceDrawerOpen, the close button becomes a no-op
       *  so the panel stays glued to the right edge for the whole session.
       *  Hidden below md by FanDrawer itself — see its pinned className. */}
      {drawerKeepOpen && drawerOpen && (
        <FanDrawer
          pinned
          alwaysOn={forceDrawerOpen}
          open={drawerOpen}
          onClose={() => { if (!forceDrawerOpen) setDrawerOpen(false); }}
          accountId={accountId}
          fanId={fanId}
          chat={chat}
          onComposeWith={(t) => composerApiRef.current?.applyTemplate(t)}
        />
      )}
    </div>
  );
}

/** Header "👥 group" button. Routes via the BroadcastChannel coord
 *  layer so clicks NEVER navigate the source tab — including the case
 *  where the source IS a popout (`/chat/<acct>/<fan>`). If a /group
 *  tab is alive AND has room, the click pushes a slot into it. If the
 *  current group tab is full (8 slots), a fresh group tab is spawned
 *  with this one fan as its first slot. */
function GroupChatButton({ accountId, fanId }: { accountId: string; fanId: number }) {
  const [flash, setFlash] = useState<null | "added" | "opened" | "focused">(null);
  const onClick = async () => {
    const res = await openGroupTab(accountId, fanId);
    // "self" means the caller is already inside /group. Don't flash —
    // there's no state change to announce, and "added" was misleading.
    if (res.kind === "self") return;
    setFlash(
      res.kind === "opened" ? "opened"
        : res.kind === "focused" ? "focused"
        : "added",
    );
    // Popout self-close: when this surface is the standalone /chat
    // popout (URL = /chat/<acct>/<fan>) AND the click resolved into
    // the live group tab (no new tab spawned), close the popout so
    // the user lands on whatever tab takes focus next — ideally the
    // group tab via the broadcast-focus message we just sent. The
    // 80ms delay lets that broadcast reach the group tab before this
    // window dies, otherwise some browsers fast-track focus back to
    // the opener instead of the broadcast target.
    //
    // window.close() only works because the popout link drops
    // rel="noreferrer" — popouts opened before that change can't
    // close themselves; user closes manually.
    if (
      (res.kind === "broadcast" || res.kind === "focused") &&
      typeof window !== "undefined" &&
      /^\/chat\//.test(window.location.pathname)
    ) {
      window.setTimeout(() => { try { window.close(); } catch { /* policy denial */ } }, 80);
    }
    window.setTimeout(() => setFlash(null), 1400);
  };
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "hidden md:inline text-[11px] underline underline-offset-2 transition-colors",
        flash === "added" ? "text-ok"
          : flash === "opened" ? "text-accent"
          : flash === "focused" ? "text-fg-dim"
          : "text-fg-dim hover:text-fg",
      )}
      title="Add to group chat tab"
    >
      {flash === "added" ? "✓ added"
        : flash === "opened" ? "↗ opened"
        : flash === "focused" ? "↗ in group"
        : "👥 group"}
    </button>
  );
}
