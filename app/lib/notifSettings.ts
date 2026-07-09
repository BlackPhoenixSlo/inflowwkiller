"use client";

/**
 * notifSettings — local preferences for the in-tab notification toaster.
 *
 * Persisted in localStorage so popouts inherit the same choices and the
 * user never has to reconfigure after a refresh. No server roundtrip;
 * this is purely a UI preference.
 *
 * Two orthogonal flags per type:
 *   • toast   — show a transient toast top-right when the type arrives
 *   • bubble  — render with a coloured accent ring + larger sticker
 *               (used to make tips/purchases pop relative to chatter)
 */

export type NotifTypeKey =
  | "message"
  | "purchases"
  | "tip"
  | "subscribed"
  | "commented"
  | "mentioned"
  | "favorited";

export const NOTIF_TYPES: { key: NotifTypeKey; label: string; icon: string }[] = [
  { key: "message",    label: "Messages",     icon: "💬" },
  { key: "purchases",  label: "Purchases",    icon: "💰" },
  { key: "tip",        label: "Tips",         icon: "💸" },
  { key: "subscribed", label: "Subscribers",  icon: "⭐" },
  { key: "mentioned",  label: "Mentions",     icon: "@" },
  { key: "commented",  label: "Comments",     icon: "💭" },
  { key: "favorited",  label: "Likes",        icon: "♡" },
];

export interface NotifSettings {
  /** Master switch — when false, no toasts at all. */
  enabled: boolean;
  /** Per-type toast visibility. */
  toast: Record<NotifTypeKey, boolean>;
  /** Per-type "bubble" emphasis (larger sticker + coloured ring). */
  bubble: Record<NotifTypeKey, boolean>;
  /** Use the OS-level Notification API when the tab is not focused. */
  osPing: boolean;
}

export const DEFAULT_NOTIF_SETTINGS: NotifSettings = {
  enabled: true,
  toast: {
    message: true,
    purchases: true,
    tip: true,
    subscribed: true,
    mentioned: true,
    commented: false,
    favorited: false,
  },
  bubble: {
    message: false,
    purchases: true,
    tip: true,
    subscribed: false,
    mentioned: false,
    commented: false,
    favorited: false,
  },
  osPing: false,
};

const STORAGE_KEY = "chatterly:notif-settings:v1";

export function readNotifSettings(): NotifSettings {
  if (typeof window === "undefined") return DEFAULT_NOTIF_SETTINGS;
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return DEFAULT_NOTIF_SETTINGS;
    const parsed = JSON.parse(raw) as Partial<NotifSettings>;
    return {
      enabled: parsed.enabled ?? DEFAULT_NOTIF_SETTINGS.enabled,
      toast: { ...DEFAULT_NOTIF_SETTINGS.toast, ...(parsed.toast ?? {}) },
      bubble: { ...DEFAULT_NOTIF_SETTINGS.bubble, ...(parsed.bubble ?? {}) },
      osPing: parsed.osPing ?? DEFAULT_NOTIF_SETTINGS.osPing,
    };
  } catch {
    return DEFAULT_NOTIF_SETTINGS;
  }
}

export function writeNotifSettings(s: NotifSettings): void {
  try { window.localStorage.setItem(STORAGE_KEY, JSON.stringify(s)); } catch { /* ignore */ }
  // Defer the dispatch so subscribers (other instances of the hook in
  // the bell + toaster) don't run their setSettings while the original
  // caller is still inside React's render/commit phase. queueMicrotask
  // lets the current task finish first.
  try {
    queueMicrotask(() => {
      try { window.dispatchEvent(new CustomEvent("chatterly:notif-settings")); } catch { /* ignore */ }
    });
  } catch { /* ignore */ }
}

// ── last-used bell filter ─────────────────────────────────────────────
//
// The bell dropdown's active filter chip (All / Tips / Purchases / …).
// Persisted so a chatter who lives on the Tips or Purchases view doesn't
// have to re-click it after every reload / popout.

const FILTER_KEY = "chatterly:notif-filter:v1";

export function readNotifFilterId(): string | null {
  if (typeof window === "undefined") return null;
  try { return window.localStorage.getItem(FILTER_KEY); } catch { return null; }
}

export function writeNotifFilterId(id: string): void {
  try { window.localStorage.setItem(FILTER_KEY, id); } catch { /* ignore */ }
}

/** Maps OF notification.type values into our internal NotifTypeKey set. */
export function mapOfTypeToKey(rawType: string | undefined | null): NotifTypeKey | null {
  if (!rawType) return null;
  const t = rawType.toLowerCase();
  if (t === "message" || t === "messages" || t === "chatmessage") return "message";
  if (t === "tip" || t === "tips") return "tip";
  if (t === "purchase" || t === "purchases" || t === "paided_message") return "purchases";
  if (t === "subscribed" || t === "subscriber" || t === "subscription") return "subscribed";
  if (t === "commented" || t === "comment") return "commented";
  if (t === "mentioned" || t === "mention") return "mentioned";
  if (t === "favorited" || t === "favorite" || t === "like" || t === "liked") return "favorited";
  return null;
}

// ── local notification history ────────────────────────────────────────
//
// OF's persisted /users/notifications list omits some event types (likes
// in particular — confirmed by hitting the relay: even on accounts with
// favorited count = 51, the list endpoint returns []). The SSE `toasts`
// stream does push them, so we mirror those into localStorage to back the
// bell's dropdown — that way the user can still see recent likes /
// mentions / etc. in the list, not just the transient toast.
//
// Capped to last 100 per account; entries older than 7d are pruned on
// every read. Shared across popouts via storage event.

export interface CachedNotification {
  id: string;
  accountId: string;
  /** Raw OF type string ("favorited", "paided_message", …). */
  type: string;
  typeKey: NotifTypeKey | null;
  user: {
    id?: number;
    name?: string;
    username?: string;
    avatar?: string;
  };
  /** Raw HTML text (anchors, img, etc) — bell renders this with the same
   *  strip-tags logic as live toasts. */
  text: string;
  replacePairs?: Record<string, string>;
  /** ISO timestamp. */
  createdAt: string;
}

const HISTORY_KEY = "chatterly:notif-history:v1";
const HISTORY_MAX_PER_ACCOUNT = 100;
// EVICT-BY: created-at — see plan/17_cache_strategy.md §7
const HISTORY_TTL_MS = 7 * 24 * 60 * 60 * 1000;

type HistoryMap = Record<string, CachedNotification[]>;

function prune(items: CachedNotification[]): CachedNotification[] {
  const cutoff = Date.now() - HISTORY_TTL_MS;
  return items
    .filter((n) => {
      const t = Date.parse(n.createdAt);
      return Number.isFinite(t) && t >= cutoff;
    })
    .sort((a, b) => Date.parse(b.createdAt) - Date.parse(a.createdAt))
    .slice(0, HISTORY_MAX_PER_ACCOUNT);
}

export function readNotifHistory(): HistoryMap {
  if (typeof window === "undefined") return {};
  try {
    const raw = window.localStorage.getItem(HISTORY_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw) as HistoryMap;
    const out: HistoryMap = {};
    for (const [aid, list] of Object.entries(parsed)) {
      if (Array.isArray(list)) out[aid] = prune(list);
    }
    return out;
  } catch {
    return {};
  }
}

function writeNotifHistory(h: HistoryMap): void {
  try { window.localStorage.setItem(HISTORY_KEY, JSON.stringify(h)); } catch { /* ignore */ }
  try {
    queueMicrotask(() => {
      try { window.dispatchEvent(new CustomEvent("chatterly:notif-history")); } catch { /* ignore */ }
    });
  } catch { /* ignore */ }
}

export function appendNotifHistory(n: CachedNotification): void {
  if (typeof window === "undefined") return;
  const h = readNotifHistory();
  const list = h[n.accountId] ?? [];
  // Dedup on id — re-pushing the same notif (locale variant, double SSE)
  // shouldn't double-list.
  if (list.some((x) => x.id === n.id)) return;
  h[n.accountId] = prune([n, ...list]);
  writeNotifHistory(h);
}

/**
 * Patch the `user` field of an existing history entry. Used to backfill
 * the resolved numeric id after an async username→id lookup so the
 * bell row's href becomes canonical (no redirect detour).
 */
export function patchNotifHistoryUser(
  accountId: string,
  notifId: string,
  patch: Partial<CachedNotification["user"]>,
): void {
  if (typeof window === "undefined") return;
  const h = readNotifHistory();
  const list = h[accountId];
  if (!list) return;
  let dirty = false;
  for (let i = 0; i < list.length; i++) {
    if (list[i].id !== notifId) continue;
    list[i] = { ...list[i], user: { ...list[i].user, ...patch } };
    dirty = true;
    break;
  }
  if (dirty) {
    h[accountId] = list;
    writeNotifHistory(h);
  }
}

// ── badge events ──────────────────────────────────────────────────────
//
// Tab-local badge counter. Each tab starts at 0 on load; arrivals bump
// by 1; clicks/bell-open reset to 0. We don't share across tabs — every
// window has its own bell with its own counter, per the simplified UX
// spec.

export const NOTIF_ARRIVED_EVENT = "chatterly:notif-arrived";
export const NOTIF_CLEARED_EVENT = "chatterly:notif-cleared";

export function dispatchNotifArrived(): void {
  if (typeof window === "undefined") return;
  try { window.dispatchEvent(new CustomEvent(NOTIF_ARRIVED_EVENT)); } catch { /* ignore */ }
}

export function dispatchNotifCleared(): void {
  if (typeof window === "undefined") return;
  try { window.dispatchEvent(new CustomEvent(NOTIF_CLEARED_EVENT)); } catch { /* ignore */ }
}
