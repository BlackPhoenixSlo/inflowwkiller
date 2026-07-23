"use client";

/**
 * NotificationToaster — listens to OF's SSE `toasts` event, renders a
 * transient toast top-right, and bumps the bell badge by 1 per arrival.
 *
 * Badge ownership lives in NotificationBell (tab-local counter). This
 * component only fires `chatterly:notif-arrived` events; the bell
 * subscribes and increments. No polling, no count diffing, no
 * cross-tab baseline — every tab starts at 0 and counts forward.
 *
 * We still mirror each arrival into localStorage via appendNotifHistory
 * so the bell dropdown can display likes / mentions that OF's own
 * /notifications endpoint drops.
 */

import { useEffect, useMemo, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import type { QueryClient } from "@tanstack/react-query";

import { useScope } from "@/contexts/ScopeContext";
import { useActiveAccounts } from "@/hooks/useAccounts";
import { useDeferredMount } from "@/hooks/useDeferredMount";
import { useNotificationSettings } from "@/hooks/useNotificationSettings";
import { chatTabName, openChatTab } from "@/lib/chatPopout";
import { eventBus, type EventEnvelope } from "@/lib/events";
import { resolveChatTarget, toUtcIso, type ChatTarget } from "@/hooks/useInboxRealtime";
import { relay, proxyImage } from "@/lib/relay";
import {
  appendNotifHistory,
  dispatchNotifArrived,
  dispatchNotifCleared,
  mapOfTypeToKey,
  patchNotifHistoryUser,
  type NotifSettings,
  type NotifTypeKey,
} from "@/lib/notifSettings";

// One entry per accountId. Lives outside the component so HMR /
// StrictMode double-mounts don't lose seen-id state. Each set is
// bounded to the last MAX_SEEN_IDS arrivals so it can't grow
// unbounded for the life of the tab — older ids can't recur as
// dupes anyway (OF only re-pushes recent locale variants).
const MAX_SEEN_IDS = 500;
const seenIdsByAccount = new Map<string, Set<string>>();

// Insertion-ordered Set: record an id as seen, evicting the oldest
// once we exceed the cap.
function markSeen(seen: Set<string>, id: string): void {
  seen.add(id);
  while (seen.size > MAX_SEEN_IDS) {
    const oldest = seen.values().next().value;
    if (oldest === undefined) break;
    seen.delete(oldest);
  }
}

interface NotificationUser {
  id?: number;
  name?: string;
  username?: string;
  avatar?: string;
}
interface NotificationItem {
  id?: number | string;
  type?: string;
  text?: string;
  description?: string;
  createdAt?: string;
  user?: NotificationUser;
  fromUser?: NotificationUser;
  replacePairs?: Record<string, string>;
}

interface Toast {
  id: string;
  accountId: string;
  typeKey: NotifTypeKey | null;
  rawType: string;
  user: NotificationUser;
  text: string;
  createdAt: number;
  bubble: boolean;
  href: string | null;
  expiresAt: number;
}

const TOAST_TTL_MS = 8_000;
const MAX_TOASTS_VISIBLE = 6;

const toastListeners = new Set<(toasts: Toast[]) => void>();
let currentToasts: Toast[] = [];

function publish() {
  for (const fn of toastListeners) fn(currentToasts);
}

function pushToast(t: Toast) {
  const dedupKey = `${t.accountId}:${t.id}`;
  if (currentToasts.some((x) => `${x.accountId}:${x.id}` === dedupKey)) return;
  currentToasts = [...currentToasts, t].slice(-MAX_TOASTS_VISIBLE);
  publish();
  const ttl = Math.max(0, t.expiresAt - Date.now());
  window.setTimeout(() => dismissToast(t.accountId, t.id), ttl);
}

function dismissToast(accountId: string, id: string) {
  const before = currentToasts.length;
  currentToasts = currentToasts.filter((t) => !(t.accountId === accountId && t.id === id));
  if (currentToasts.length !== before) publish();
}

function patchToastHref(accountId: string, id: string, href: string): void {
  const idx = currentToasts.findIndex((x) => x.accountId === accountId && x.id === id);
  if (idx < 0) return;
  if (currentToasts[idx].href === href) return;
  currentToasts = currentToasts.map((t, i) => (i === idx ? { ...t, href } : t));
  publish();
}

// Username → user-id cache. SSE-pushed likes carry only the @username;
// we resolve eagerly so the toast's click target is canonical and the
// chat popout's header renders instantly.
const resolvedIdByUsername = new Map<string, number>();
const inflightLookups = new Map<string, Promise<number | null>>();

async function resolveUsernameId(
  accountId: string,
  username: string,
  qc: QueryClient,
): Promise<number | null> {
  const key = `${accountId}:${username}`;
  const cached = resolvedIdByUsername.get(key);
  if (cached) return cached;
  const inflight = inflightLookups.get(key);
  if (inflight) return inflight;

  const p = (async () => {
    try {
      const u = await relay.get<{
        id?: number | string;
        name?: string;
        username?: string;
        avatar?: string | null;
      }>(
        `/api/of/v2/users/${encodeURIComponent(username)}`,
        { accountId },
      );
      const rawId = typeof u?.id === "number" ? u.id : Number(u?.id);
      if (!Number.isFinite(rawId)) return null;
      const id = rawId as number;
      resolvedIdByUsername.set(key, id);
      qc.setQueryData(["of-user", accountId, id], (prev: { customNickname?: string | null } | undefined) => ({
        id,
        name: u.name,
        username: u.username,
        avatar: u.avatar ?? null,
        // Preserve a previously-stitched team nickname so a notification
        // resolve doesn't wipe what enrichWithUsers populated.
        customNickname: prev?.customNickname ?? null,
      }));
      return id;
    } catch {
      return null;
    } finally {
      inflightLookups.delete(key);
    }
  })();
  inflightLookups.set(key, p);
  return p;
}

function useToastStack(): Toast[] {
  const [toasts, setToasts] = useState<Toast[]>(currentToasts);
  useEffect(() => {
    const fn = (next: Toast[]) => setToasts(next);
    toastListeners.add(fn);
    return () => { toastListeners.delete(fn); };
  }, []);
  return toasts;
}

/**
 * OF's toast payload's `user` field is the recipient (the model whose
 * post was liked), not the actor (the fan). The actor's avatar lives
 * inside the HTML `text` blob as the first <img src="…"> tag.
 */
function extractAvatarFromHtml(raw: string | undefined | null): string | null {
  if (!raw || typeof raw !== "string") return null;
  const m = raw.match(/<img[^>]*\bsrc=["']([^"']+)["']/i);
  return m ? m[1] : null;
}

function extractUsernameFromHtml(raw: string | undefined | null): string | null {
  if (!raw || typeof raw !== "string") return null;
  const m = raw.match(/href=["']https?:\/\/(?:www\.)?onlyfans\.com\/([A-Za-z0-9._-]+)["']/i);
  return m ? m[1] : null;
}

function renderText(raw: string, replacePairs?: Record<string, string>): string {
  let s = raw;
  if (replacePairs) {
    for (const [k, v] of Object.entries(replacePairs)) s = s.split(k).join(v);
  }
  return s.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();
}

function hrefFor(n: NotificationItem, accountId: string): string | null {
  const u = n.user || n.fromUser || {};
  if (u.id) return `/chat/${encodeURIComponent(accountId)}/${u.id}`;
  if (u.username) {
    return `/chat/${encodeURIComponent(accountId)}/u/${encodeURIComponent(u.username)}`;
  }
  return null;
}

/** Tip / purchase toasts deep-link with a one-shot `?refresh=media` flag.
 *  The fan just moved money, so the popout's rehydrated persisted caches
 *  (PPV ledger, last-purchases, gallery, profile) are stale by seconds —
 *  the popout chat page reads the flag on mount and refetches the thread
 *  plus the FanDrawer's manual-↻ set. Other types link plain: no reason
 *  to burn the refetches on a like or a comment. */
function withRefreshFlag(href: string, typeKey: NotifTypeKey | null): string {
  if (typeKey !== "tip" && typeKey !== "purchases") return href;
  return `${href}${href.includes("?") ? "&" : "?"}refresh=media`;
}

/** One arrival, delivered. Every SSE lane below — OF's `toasts` feed, inbound
 *  DMs, PPV unlocks — differs only in how it parses its envelope; from here on
 *  the policy is identical, so it lives here once instead of three times.
 *
 *  The contract that must not drift: history is mirrored ALWAYS, pings are
 *  GATED. A muted arrival (master off, or its type toggled off) still reaches
 *  the bell dropdown and the money rail — muting popups must not make those go
 *  deaf — but produces no badge bump, no popup and no OS notification.
 *
 *  Returns whether it pinged, so a caller that batches (the `toasts` loop) can
 *  tell whether anything in the batch was user-visible.
 */
function deliverNotification(
  qc: QueryClient,
  settings: NotifSettings,
  n: {
    id: string;
    accountId: string;
    typeKey: NotifTypeKey | null;
    /** OF's own type string, kept verbatim for the history row. */
    rawType: string;
    user: NotificationUser;
    /** Rendered, for the toast. */
    text: string;
    /** Stored, for the history row — OF's lanes keep the raw HTML so the
     *  dropdown can re-render it. Defaults to `text`. */
    historyText?: string;
    replacePairs?: Record<string, string>;
    createdMs: number;
    href: string | null;
  },
): boolean {
  appendNotifHistory({
    id: n.id,
    accountId: n.accountId,
    type: n.rawType,
    typeKey: n.typeKey,
    user: {
      id: n.user.id,
      name: n.user.name,
      username: n.user.username,
      avatar: n.user.avatar,
    },
    text: n.historyText ?? n.text,
    replacePairs: n.replacePairs,
    createdAt: new Date(n.createdMs).toISOString(),
  });

  if (!settings.enabled || (n.typeKey && !settings.toast[n.typeKey])) return false;

  dispatchNotifArrived();
  pushToast({
    id: n.id,
    accountId: n.accountId,
    typeKey: n.typeKey,
    rawType: n.rawType,
    user: n.user,
    text: n.text,
    createdAt: n.createdMs,
    bubble: n.typeKey ? !!settings.bubble[n.typeKey] : false,
    href: n.href,
    expiresAt: Date.now() + TOAST_TTL_MS,
  });

  const focused = typeof document !== "undefined" ? document.hasFocus() : true;
  const perm = typeof Notification !== "undefined" ? Notification.permission : "unsupported";
  if (settings.osPing && typeof Notification !== "undefined" && !focused && perm === "granted") {
    try {
      new Notification(n.user.name || n.user.username || "Fastt", {
        body: n.text || n.rawType || "New activity",
        tag: n.id,
      });
    } catch (err) {
      console.warn("[toaster] OS Notification threw:", err);
    }
  }
  return true;
}

function NotificationDeltaPoller() {
  const { accountId } = useScope();
  const active = useActiveAccounts();
  const { settings } = useNotificationSettings();
  const ready = useDeferredMount(1_500);
  const qc = useQueryClient();

  const targetIds = useMemo<string[]>(
    () => (accountId ? [accountId] : active.map((a) => a.id)),
    [accountId, active],
  );

  // SSE: direct "toasts" event — OF pushes the full notification payload
  // here within ~1s of the underlying action. This is our only source of
  // truth now; no polling, no count-diff backstop.
  //
  // The listener runs even with the master "Enabled" toggle OFF: the history
  // mirror feeds the bell dropdown AND the Buys & tips rail, and muting
  // popups must not make those go deaf. `enabled` gates the pings below
  // (toast + badge + OS), not the pipeline.
  useEffect(() => {
    if (!ready) return;
    const off = eventBus.on("toasts", (env: EventEnvelope) => {
      const aid = env.__account_id;
      if (!aid || !targetIds.includes(aid)) return;
      const arr = (env as { toasts?: unknown }).toasts;
      if (!Array.isArray(arr)) return;

      let handledAny = false;
      for (const raw of arr) {
        if (!raw || typeof raw !== "object") continue;
        const t = raw as Record<string, unknown>;
        const idVal = t.id;
        if (idVal == null) continue;
        const id = String(idVal);

        // Dedup on the notification ID. OF's locale variants share the
        // same id so we only count each arrival once.
        const seen = seenIdsByAccount.get(aid) ?? new Set<string>();
        if (seen.has(id)) continue;
        markSeen(seen, id);
        seenIdsByAccount.set(aid, seen);

        const typeKey = mapOfTypeToKey(typeof t.type === "string" ? t.type : null);

        const rawText = (typeof t.text === "string" ? t.text : "")
          || (typeof t.description === "string" ? t.description : "")
          || (typeof t.title === "string" ? t.title : "");
        const data = (t.data && typeof t.data === "object")
          ? (t.data as { relatedUser?: Record<string, unknown>; replacements?: Record<string, string> })
          : undefined;
        const relatedUser = data?.relatedUser;
        const repl = data?.replacements;
        const fromReplId = repl?.["{RELATED_USER_ID}"];
        const fromReplName = repl?.["{RELATED_USER_NAME}"];
        const fromReplLogin = repl?.["{RELATED_USER_LOGIN}"];
        const fromReplAvatar = repl?.["{RELATED_USER_AVATAR}"];

        const fromUser = (t.fromUser as NotificationUser | undefined);
        const recipientUser = (t.user as NotificationUser | undefined);
        const htmlAvatar = extractAvatarFromHtml(rawText);
        const htmlUsername = extractUsernameFromHtml(rawText);

        const relatedAvatarObj = relatedUser?.avatarThumbs as
          | { c50?: string; c144?: string }
          | undefined;
        const relatedAvatar =
          relatedAvatarObj?.c144 ||
          relatedAvatarObj?.c50 ||
          (typeof relatedUser?.avatar === "string" ? relatedUser.avatar : undefined);

        const relatedIdRaw = relatedUser?.id ?? (fromReplId ? Number(fromReplId) : undefined);
        const relatedId =
          typeof relatedIdRaw === "number" && Number.isFinite(relatedIdRaw)
            ? relatedIdRaw
            : undefined;

        const user: NotificationUser = {
          id: relatedId ?? fromUser?.id,
          name:
            (typeof relatedUser?.name === "string" ? relatedUser.name : undefined) ||
            fromReplName ||
            fromUser?.name ||
            recipientUser?.name,
          username:
            (typeof relatedUser?.username === "string" ? relatedUser.username : undefined) ||
            fromReplLogin ||
            fromUser?.username ||
            htmlUsername ||
            recipientUser?.username,
          avatar:
            relatedAvatar ||
            fromReplAvatar ||
            fromUser?.avatar ||
            htmlAvatar ||
            undefined,
        };

        if (user.id != null) {
          qc.setQueryData(["of-user", aid, user.id], (prev: { customNickname?: string | null } | undefined) => ({
            id: user.id,
            name: user.name,
            username: user.username,
            avatar: user.avatar ?? null,
            customNickname: prev?.customNickname ?? null,
          }));
        }
        const text = renderText(rawText, repl ?? (t.replacePairs as Record<string, string> | undefined));
        const createdMs = typeof t.createdAt === "string"
          ? Date.parse(t.createdAt) || Date.now()
          : Date.now();

        const baseHref = hrefFor({ user } as NotificationItem, aid);
        // History always, pings gated — the money rail and the feed dropdown
        // must still list a muted arrival (incl. likes).
        deliverNotification(qc, settings, {
          id,
          accountId: aid,
          typeKey,
          rawType: typeof t.type === "string" ? t.type : "",
          user,
          text,
          historyText: rawText,
          replacePairs: repl ?? (t.replacePairs as Record<string, string> | undefined),
          createdMs,
          href: baseHref ? withRefreshFlag(baseHref, typeKey) : null,
        });
        // Deliberately NOT gated on whether it pinged: this lane refetches the
        // notif-list for anything it took in, muted or not, so a silenced
        // arrival still refreshes the rail from OF's own feed.
        handledAny = true;

        // Resolve username → id in the background whether or not it pinged;
        // the history entry gets patched either way. Only OF's own toasts
        // arrive without a user id, which is why this lane alone needs it.
        if (!user.id && user.username) {
          const uname = user.username;
          resolveUsernameId(aid, uname, qc).then((resolvedId) => {
            if (resolvedId == null) return;
            const canonical = withRefreshFlag(
              `/chat/${encodeURIComponent(aid)}/${resolvedId}`,
              typeKey,
            );
            patchToastHref(aid, id, canonical);
            patchNotifHistoryUser(aid, id, { id: resolvedId });
          }).catch(() => { /* ignore */ });
        }
      }

      if (handledAny) {
        qc.invalidateQueries({ queryKey: ["notif-list", aid], exact: false });
      }
    });
    return off;
  }, [qc, settings, ready, targetIds]);

  // SSE: inbound chat messages — OF's native `toasts` feed does NOT carry
  // DMs (those arrive on `api2_chat_message`, consumed by useInboxRealtime
  // for the chat cache), so without this a new webhook message never
  // produced a toast/bubble. Mirror inbound messages into the same toast
  // pipeline: shown when the per-type "message" Toast toggle is on, and
  // rendered as an accent-ring "bubble" when `settings.bubble.message` is
  // on. Outbound (our own sends / AI replies / mass / funnel) never ping.
  useEffect(() => {
    // Same as the `toasts` handler: the mirror keeps running with the master
    // toggle off; only the pings below are gated.
    if (!ready) return;
    const off = eventBus.on("api2_chat_message", (env: EventEnvelope) => {
      const target: ChatTarget | null = resolveChatTarget(
        env as unknown as Parameters<typeof resolveChatTarget>[0],
      );
      if (!target) return;
      const { accountId: aid, fanId, isOutbound, msg, fromId } = target;
      if (isOutbound) return;                 // only a fan's inbound DM pings
      if (!targetIds.includes(aid)) return;   // in-scope accounts only

      // Namespace the id with "m" so a message can't collide with an OF
      // notification id in the shared per-account seen set / toast dedup.
      const id = "m" + String(msg.id);
      const seen = seenIdsByAccount.get(aid) ?? new Set<string>();
      if (seen.has(id)) return;
      markSeen(seen, id);
      seenIdsByAccount.set(aid, seen);

      const fromUser = (msg.fromUser ?? {}) as NotificationUser;
      const user: NotificationUser = {
        id: fromId,
        name: fromUser.name || fromUser.username,
        username: fromUser.username,
        avatar: fromUser.avatar,
      };
      if (user.id != null) {
        qc.setQueryData(["of-user", aid, user.id], (prev: { customNickname?: string | null } | undefined) => ({
          id: user.id,
          name: user.name,
          username: user.username,
          avatar: user.avatar ?? null,
          customNickname: prev?.customNickname ?? null,
        }));
      }

      const rawText = (typeof msg.text === "string" ? msg.text : "").trim();
      const text =
        renderText(rawText) ||
        ((msg.mediaCount ?? 0) > 0 ? "Sent you media" : "Sent you a message");
      const createdMs = msg.createdAt
        ? Date.parse(toUtcIso(msg.createdAt)) || Date.now()
        : Date.now();

      // History is mirrored REGARDLESS of the toggles (see deliverNotification),
      // so the bell dropdown still lists DMs with toasts off — OF's
      // /notifications feed lists them inconsistently, so this is their only
      // reliable route into the dropdown.
      const pinged = deliverNotification(qc, settings, {
        id,
        accountId: aid,
        typeKey: "message",
        rawType: "message",
        user,
        text,
        historyText: rawText || text,
        createdMs,
        href: `/chat/${encodeURIComponent(aid)}/${fanId}`,
      });
      if (pinged) qc.invalidateQueries({ queryKey: ["notif-list", aid], exact: false });
    });
    return off;
  }, [qc, settings, ready, targetIds]);

  // SSE: a PPV unlock. The ONLY money event with no push path of its own —
  // OF sends no purchase frame and no purchase toast (verified against the
  // full raw-event window), so the `toasts` handler above never fires for a
  // buy and, before this, a sale produced no toast, no badge, no bell entry,
  // and no rail row until someone reloaded the page.
  //
  // The relay announces it off OF's purchases-notification feed — the fresh
  // source (~30s). Deliberately NOT the transactions ledger: that is the money
  // record, but it lands late and by its own measurement sometimes hours late,
  // which is useless for "he just bought".
  //
  // `notif_id` is OF's REAL notification id, which is what makes this safe:
  // the rail and bell list that same feed, and both dedupe on the id, so this
  // live ping and the eventual feed row collapse into ONE row instead of
  // showing the sale twice.
  useEffect(() => {
    if (!ready) return;
    const off = eventBus.on("purchase_notified", (env: EventEnvelope) => {
      const aid = env.__account_id;
      if (!aid || !targetIds.includes(aid)) return;
      const p = env.purchase_notified as {
        notif_id?: string; fan_id?: number; amount_cents?: number;
        created_at?: string | null; name?: string | null;
      } | undefined;
      const fanId = Number(p?.fan_id);
      if (!p?.notif_id || !Number.isFinite(fanId)) return;

      // OF's own id, unprefixed — it MUST equal the id the feed will carry
      // for this purchase, or the dedupe this whole design rests on fails.
      const id = String(p.notif_id);
      const seen = seenIdsByAccount.get(aid) ?? new Set<string>();
      if (seen.has(id)) return;
      markSeen(seen, id);
      seenIdsByAccount.set(aid, seen);

      // The notification names the payer; fall back to the ["of-user"] cache
      // the message handler above populates.
      const cached = qc.getQueryData(["of-user", aid, fanId]) as NotificationUser | undefined;
      const user: NotificationUser = {
        id: fanId,
        name: p.name || cached?.name,
        username: cached?.username,
        avatar: cached?.avatar,
      };

      const cents = Number(p.amount_cents);
      const text = Number.isFinite(cents) && cents > 0
        ? `Unlocked your PPV — $${(cents / 100).toFixed(2)}`
        : "Unlocked your PPV";
      // OF stamps these with an explicit offset ("…+00:00"); toUtcIso passes
      // a zoned timestamp through untouched.
      const createdMs = p.created_at
        ? Date.parse(toUtcIso(p.created_at)) || Date.now()
        : Date.now();

      const pinged = deliverNotification(qc, settings, {
        id,
        accountId: aid,
        typeKey: "purchases",
        rawType: "purchases",
        user,
        text,
        createdMs,
        // Same one-shot refresh flag the money toasts and rail rows carry —
        // the fan just moved money, so the popout's caches are stale.
        href: withRefreshFlag(`/chat/${encodeURIComponent(aid)}/${fanId}`, "purchases"),
      });
      if (pinged) qc.invalidateQueries({ queryKey: ["notif-list", aid], exact: false });
    });
    return off;
  }, [qc, settings, ready, targetIds]);

  // Request OS permission lazily once the user has flipped osPing on.
  useEffect(() => {
    if (!settings.osPing) return;
    if (typeof Notification === "undefined") return;
    if (Notification.permission === "default") {
      try { Notification.requestPermission().catch(() => {}); } catch { /* ignore */ }
    }
  }, [settings.osPing]);

  return null;
}

function ToastCard({ t }: { t: Toast }) {
  const avatar = proxyImage(t.user.avatar ?? null, t.accountId);
  const ring = t.bubble
    ? "ring-2 ring-accent shadow-[0_0_24px_-4px_var(--color-accent)]"
    : "ring-1 ring-border";
  const sticker = t.bubble ? "text-base" : "text-xs";
  const body = (
    <div
      className={
        "pointer-events-auto w-auto sm:w-[320px] bg-panel rounded-lg overflow-hidden flex items-start gap-2.5 p-2.5 " +
        ring
      }
    >
      <div className="w-9 h-9 rounded-full bg-bg-elev-1 overflow-hidden shrink-0 grid place-items-center relative">
        {avatar ? (
          <img src={avatar} alt="" loading="lazy" decoding="async" className="w-full h-full object-cover" />
        ) : (
          <span className="text-[11px] text-fg-dim">
            {(t.user.name || t.user.username || "?").slice(0, 1).toUpperCase()}
          </span>
        )}
        <span className={`absolute -bottom-1 -right-1 ${sticker}`} aria-hidden>
          {stickerFor(t.typeKey)}
        </span>
      </div>
      <div className="min-w-0 flex-1">
        <div className="text-[12px] text-fg leading-snug">
          {t.user.name && <span className="font-medium">{t.user.name} </span>}
          <span className="text-fg-dim">{t.text}</span>
        </div>
        <div className="text-[10px] text-fg-dim mt-0.5 flex items-center justify-between gap-2">
          <span>{labelFor(t.typeKey, t.rawType)}</span>
          {t.href && (
            <span className="text-fg-dim/80 hover:text-fg underline underline-offset-2">
              Open chat ↗
            </span>
          )}
        </div>
      </div>
      <button
        type="button"
        onClick={(e) => { e.preventDefault(); e.stopPropagation(); dismissToast(t.accountId, t.id); }}
        className="text-fg-dim hover:text-fg text-[11px] w-10 h-10 sm:w-5 sm:h-5 grid place-items-center rounded hover:bg-bg-elev-1"
        aria-label="Dismiss"
      >
        ✕
      </button>
    </div>
  );
  if (t.href) {
    const href = t.href;
    return (
      <a
        href={href}
        target={chatTabName(href)}
        className="block animate-toast-in"
        onClick={(e) => {
          openChatTab(e, href);
          dispatchNotifCleared();
          dismissToast(t.accountId, t.id);
        }}
      >
        {body}
      </a>
    );
  }
  return <div className="animate-toast-in">{body}</div>;
}

function stickerFor(k: NotifTypeKey | null): string {
  switch (k) {
    case "message":    return "💬";
    case "purchases":  return "💰";
    case "tip":        return "💸";
    case "subscribed": return "⭐";
    case "mentioned":  return "@";
    case "commented":  return "💭";
    case "favorited":  return "♡";
    default:           return "🔔";
  }
}

function labelFor(k: NotifTypeKey | null, raw: string): string {
  switch (k) {
    case "message":    return "New message";
    case "purchases":  return "Purchase";
    case "tip":        return "Tip received";
    case "subscribed": return "New subscriber";
    case "mentioned":  return "Mentioned you";
    case "commented":  return "Commented";
    case "favorited":  return "Liked";
    default:           return raw || "Notification";
  }
}

function ToastStack() {
  const toasts = useToastStack();
  if (toasts.length === 0) return null;
  return (
    <div className="fixed top-14 left-2 right-2 sm:left-auto sm:right-3 z-[60] flex flex-col gap-2 pointer-events-none [&>*:nth-child(n+3)]:hidden sm:[&>*:nth-child(n+3)]:block">
      {toasts.slice().reverse().map((t) => (
        <ToastCard key={`${t.accountId}:${t.id}`} t={t} />
      ))}
    </div>
  );
}

export function NotificationToaster() {
  return (
    <>
      <NotificationDeltaPoller />
      <ToastStack />
    </>
  );
}
