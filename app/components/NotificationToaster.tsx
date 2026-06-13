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
import { eventBus, type EventEnvelope } from "@/lib/events";
import { relay, proxyImage } from "@/lib/relay";
import {
  appendNotifHistory,
  dispatchNotifArrived,
  dispatchNotifCleared,
  mapOfTypeToKey,
  patchNotifHistoryUser,
  type NotifTypeKey,
} from "@/lib/notifSettings";

// One entry per accountId. Lives outside the component so HMR /
// StrictMode double-mounts don't lose seen-id state.
const seenIdsByAccount = new Map<string, Set<string>>();

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
  useEffect(() => {
    if (!settings.enabled || !ready) return;
    const off = eventBus.on("toasts", (env: EventEnvelope) => {
      const aid = env.__account_id;
      if (!aid || !targetIds.includes(aid)) return;
      const arr = (env as { toasts?: unknown }).toasts;
      if (!Array.isArray(arr)) return;

      let pushedAny = false;
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
        seen.add(id);
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

        // Always mirror into history + bump the badge — independent of
        // whether the user has toasts enabled for this type. The toast
        // popup itself is what the per-type filter gates.
        appendNotifHistory({
          id,
          accountId: aid,
          type: typeof t.type === "string" ? t.type : "",
          typeKey,
          user: {
            id: user.id,
            name: user.name,
            username: user.username,
            avatar: user.avatar,
          },
          text: rawText,
          replacePairs: repl ?? (t.replacePairs as Record<string, string> | undefined),
          createdAt: new Date(createdMs).toISOString(),
        });
        dispatchNotifArrived();
        pushedAny = true;

        // Per-type filter for the visible toast popup only.
        if (typeKey && !settings.toast[typeKey]) {
          // Resolve username → id in the background even if we didn't
          // toast — the history entry will be patched.
          if (!user.id && user.username) {
            const uname = user.username;
            resolveUsernameId(aid, uname, qc).then((resolvedId) => {
              if (resolvedId == null) return;
              patchNotifHistoryUser(aid, id, { id: resolvedId });
            }).catch(() => { /* ignore */ });
          }
          continue;
        }

        const now = Date.now();
        pushToast({
          id,
          accountId: aid,
          typeKey,
          rawType: typeof t.type === "string" ? t.type : "",
          user,
          text,
          createdAt: createdMs,
          bubble: typeKey ? !!settings.bubble[typeKey] : false,
          href: hrefFor({ user } as NotificationItem, aid),
          expiresAt: now + TOAST_TTL_MS,
        });

        if (!user.id && user.username) {
          const uname = user.username;
          resolveUsernameId(aid, uname, qc).then((resolvedId) => {
            if (resolvedId == null) return;
            const canonical = `/chat/${encodeURIComponent(aid)}/${resolvedId}`;
            patchToastHref(aid, id, canonical);
            patchNotifHistoryUser(aid, id, { id: resolvedId });
          }).catch(() => { /* ignore */ });
        }

        const focused = typeof document !== "undefined" ? document.hasFocus() : true;
        const perm = typeof Notification !== "undefined" ? Notification.permission : "unsupported";
        if (settings.osPing && typeof Notification !== "undefined" && !focused && perm === "granted") {
          try {
            new Notification(user.name || user.username || "Fastt", {
              body: text || (typeof t.type === "string" ? t.type : "New activity"),
              tag: id,
            });
          } catch (err) {
            console.warn("[toaster] OS Notification threw:", err);
          }
        }
      }

      if (pushedAny) {
        qc.invalidateQueries({ queryKey: ["notif-list", aid], exact: false });
      }
    });
    return off;
  }, [qc, settings.enabled, settings.toast, settings.bubble, settings.osPing, ready, targetIds]);

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
    ? "ring-2 ring-accent shadow-[0_0_24px_-4px_var(--accent)]"
    : "ring-1 ring-border";
  const sticker = t.bubble ? "text-base" : "text-xs";
  const body = (
    <div
      className={
        "pointer-events-auto w-[320px] bg-panel rounded-lg overflow-hidden flex items-start gap-2.5 p-2.5 " +
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
        className="text-fg-dim hover:text-fg text-[11px] w-5 h-5 grid place-items-center rounded hover:bg-bg-elev-1"
        aria-label="Dismiss"
      >
        ✕
      </button>
    </div>
  );
  if (t.href) {
    return (
      <a
        href={t.href}
        target="_blank"
        rel="noreferrer"
        className="block animate-toast-in"
        onClick={() => {
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
    <div className="fixed top-14 right-3 z-[60] flex flex-col gap-2 pointer-events-none">
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
