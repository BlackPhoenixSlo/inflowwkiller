"use client";

/**
 * NotificationBell — top-nav bell with a tab-local counter.
 *
 * Badge model (simplified):
 *   • Starts at 0 on every tab load / reload.
 *   • Bumps +1 on every `chatterly:notif-arrived` window event
 *     (dispatched by the toaster on each SSE arrival).
 *   • Resets to 0 when the bell is opened, when the user clicks a row
 *     in the dropdown, or when a toast is clicked elsewhere
 *     (`chatterly:notif-cleared` event).
 *
 * Popouts each maintain their own counter — no localStorage sync, no
 * cross-tab math. The previous baseline + pending-SSE machinery was
 * dropped along with the badge complexity that motivated it.
 *
 * The dropdown's list view still queries OF (when open) and merges with
 * the localStorage history mirror so types OF drops (likes etc.) still
 * show up.
 */

import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import { useQueries } from "@tanstack/react-query";

import { useScope } from "@/contexts/ScopeContext";
import { useActiveAccounts } from "@/hooks/useAccounts";
import { useNotificationSettings } from "@/hooks/useNotificationSettings";
import { relay, proxyImage } from "@/lib/relay";
import {
  NOTIF_ARRIVED_EVENT,
  NOTIF_CLEARED_EVENT,
  NOTIF_TYPES,
  dispatchNotifCleared,
  mapOfTypeToKey,
  readNotifFilterId,
  readNotifHistory,
  writeNotifFilterId,
  type CachedNotification,
  type NotifTypeKey,
} from "@/lib/notifSettings";

type Filter = {
  id: string;
  label: string;
  /** OF `type=` value, or null for "All" (omit param). */
  type: string | null;
};
const FILTERS: Filter[] = [
  { id: "all",         label: "All",           type: null         },
  { id: "subscribed",  label: "Subscriptions", type: "subscribed" },
  { id: "purchases",   label: "Purchases",     type: "purchases"  },
  { id: "tip",         label: "Tips",          type: "tip"        },
  { id: "commented",   label: "Comments",      type: "commented"  },
  { id: "mentioned",   label: "Mentions",      type: "mentioned"  },
  { id: "favorited",   label: "Likes",         type: "favorited"  },
  { id: "message",     label: "Messages",      type: "message"    },
];

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
  [k: string]: unknown;
}

interface MergedItem {
  n: NotificationItem;
  accountId: string;
  ts: number;
}

function notifPath(type: string | null): string {
  const q = new URLSearchParams({ limit: "20", offset: "0" });
  if (type) q.set("type", type);
  return `/api/of/v2/users/notifications?${q.toString()}`;
}

function parseList(raw: unknown): NotificationItem[] {
  if (Array.isArray(raw)) return raw as NotificationItem[];
  if (raw && typeof raw === "object" && Array.isArray((raw as { list?: unknown }).list)) {
    return (raw as { list: NotificationItem[] }).list;
  }
  return [];
}

export function NotificationBell() {
  const { accountId } = useScope();
  const activeAccounts = useActiveAccounts();
  const [open, setOpen] = useState(false);
  // Restore the last-used filter chip. Safe against hydration mismatch:
  // the dropdown (the only place `filter` renders) is closed at SSR time.
  const [filter, setFilter] = useState<Filter>(() => {
    const id = readNotifFilterId();
    return FILTERS.find((f) => f.id === id) ?? FILTERS[0];
  });
  const pickFilter = (f: Filter) => {
    setFilter(f);
    writeNotifFilterId(f.id);
  };
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [osTestMsg, setOsTestMsg] = useState<string | null>(null);
  const [badge, setBadge] = useState(0);
  const ref = useRef<HTMLDivElement | null>(null);
  const openRef = useRef(open);
  openRef.current = open;
  const { settings, setToast, setBubble, update } = useNotificationSettings();

  const targetAccountIds = useMemo<string[]>(
    () => (accountId ? [accountId] : activeAccounts.map((a) => a.id)),
    [accountId, activeAccounts],
  );

  // Badge counter — bump on every arrival (unless the panel is open and the
  // user is already looking at it), reset on every cleared event.
  useEffect(() => {
    const onArrived = () => {
      if (openRef.current) return;
      setBadge((n) => Math.min(999, n + 1));
    };
    const onCleared = () => setBadge(0);
    window.addEventListener(NOTIF_ARRIVED_EVENT, onArrived);
    window.addEventListener(NOTIF_CLEARED_EVENT, onCleared);
    return () => {
      window.removeEventListener(NOTIF_ARRIVED_EVENT, onArrived);
      window.removeEventListener(NOTIF_CLEARED_EVENT, onCleared);
    };
  }, []);

  // Opening the bell counts as "seen everything".
  useEffect(() => {
    if (open) {
      setBadge(0);
      dispatchNotifCleared();
    }
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onClick = (e: MouseEvent) => {
      if (!ref.current?.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("pointerdown", onClick);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("pointerdown", onClick);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  // List — fan-out, merged client-side. Enabled only when the panel is
  // open so we don't spam OF on idle pages.
  const listQueries = useQueries({
    queries: targetAccountIds.map((aid) => ({
      queryKey: ["notif-list", aid, filter.type] as const,
      queryFn: async () => parseList(await relay.get(notifPath(filter.type), { accountId: aid })),
      enabled: open,
      staleTime: 30_000,
      refetchOnWindowFocus: false,
    })),
  });

  // Re-render when localStorage toast history changes (SSE pushes new
  // entries to it; popouts share via the storage event).
  const [historyTick, bumpHistory] = useState(0);
  useEffect(() => {
    if (!open) return;
    const bump = () => bumpHistory((n) => n + 1);
    const onStorage = (e: StorageEvent) => {
      if (e.key === "chatterly:notif-history:v1") bump();
    };
    window.addEventListener("chatterly:notif-history", bump);
    window.addEventListener("storage", onStorage);
    return () => {
      window.removeEventListener("chatterly:notif-history", bump);
      window.removeEventListener("storage", onStorage);
    };
  }, [open]);

  // Stable signature of the fetched data — the query array itself is a fresh
  // reference every render, so the memo below keys off the per-query update
  // timestamps (which change only when the underlying data does) instead.
  const dataSig = listQueries.map((q) => q.dataUpdatedAt).join(",");

  const merged = useMemo<MergedItem[]>(() => {
    const acc: MergedItem[] = [];
    const seenIds = new Set<string>();
    listQueries.forEach((q, i) => {
      const aid = targetAccountIds[i];
      if (!aid) return;
      for (const n of q.data ?? []) {
        const ts = n.createdAt ? Date.parse(n.createdAt) || 0 : 0;
        if (n.id != null) seenIds.add(`${aid}:${n.id}`);
        acc.push({ n, accountId: aid, ts });
      }
    });
    const history = readNotifHistory();
    const wantKey: NotifTypeKey | null = filter.id === "all"
      ? null
      : (filter.id as NotifTypeKey);
    for (const aid of targetAccountIds) {
      const items: CachedNotification[] = history[aid] ?? [];
      for (const c of items) {
        if (seenIds.has(`${aid}:${c.id}`)) continue;
        const key = c.typeKey ?? mapOfTypeToKey(c.type);
        if (wantKey && key !== wantKey) continue;
        const ts = c.createdAt ? Date.parse(c.createdAt) || 0 : 0;
        const n: NotificationItem = {
          id: c.id,
          type: c.type,
          text: c.text,
          createdAt: c.createdAt,
          user: c.user,
          replacePairs: c.replacePairs,
        };
        acc.push({ n, accountId: aid, ts });
      }
    }
    acc.sort((a, b) => b.ts - a.ts);
    return acc.slice(0, 50);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dataSig, targetAccountIds, filter.id, historyTick]);

  const anyLoading = open && listQueries.some((q) => q.isLoading);
  const firstError = listQueries.find((q) => q.error)?.error as Error | undefined;
  const showAccountTag = targetAccountIds.length > 1;
  const accountLabel = useAccountLabelMap(activeAccounts);

  return (
    <div className="relative" ref={ref}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="relative w-8 h-8 grid place-items-center rounded-lg text-sm bg-bg-elev-1 hover:bg-bg-elev-2 border border-border"
        title="Notifications"
        aria-label="Notifications"
      >
        <span>🔔</span>
        {badge > 0 && (
          <span className="absolute -top-1 -right-1 min-w-[16px] h-4 px-1 grid place-items-center rounded-full bg-accent text-white text-[9px] font-semibold">
            {badge > 99 ? "99+" : badge}
          </span>
        )}
      </button>
      {open && (
        <div className="fixed inset-x-2 top-14 w-auto max-h-[70vh] sm:absolute sm:inset-x-auto sm:right-0 sm:top-auto sm:mt-1 sm:w-[380px] sm:max-h-[480px] bg-panel border border-border rounded-lg shadow-xl z-40 flex flex-col">
          {targetAccountIds.length === 0 ? (
            <div className="p-4 text-xs text-fg-dim">
              No active sessions. Capture one in Setup to see notifications.
            </div>
          ) : (
            <>
              <div className="border-b border-border p-2 flex items-start gap-1">
                <div className="flex flex-wrap gap-1 flex-1 min-w-0">
                  {FILTERS.map((f) => (
                    <button
                      key={f.id}
                      type="button"
                      onClick={() => pickFilter(f)}
                      className={
                        "px-3 py-2 text-xs min-h-[36px] sm:px-2 sm:py-0.5 sm:text-[11px] sm:leading-normal sm:min-h-0 rounded-full border " +
                        (filter.id === f.id
                          ? "bg-accent text-white border-accent"
                          : "border-border text-fg-dim hover:text-fg hover:bg-bg-elev-1")
                      }
                    >
                      {f.label}
                    </button>
                  ))}
                </div>
                <button
                  type="button"
                  onClick={() => setSettingsOpen((v) => !v)}
                  className={
                    "shrink-0 w-9 h-9 sm:w-6 sm:h-6 grid place-items-center rounded text-sm sm:text-[11px] border " +
                    (settingsOpen
                      ? "border-accent text-fg bg-bg-elev-1"
                      : "border-border text-fg-dim hover:text-fg hover:bg-bg-elev-1")
                  }
                  title="Toast settings"
                  aria-label="Toast settings"
                >
                  ⚙
                </button>
              </div>
              {settingsOpen && (
                <div className="border-b border-border p-2.5 bg-bg-elev-1/30">
                  <div className="flex items-center justify-between mb-2 gap-2">
                    <span className="text-[11px] font-medium text-fg">Toast settings</span>
                    <div className="flex items-center gap-2">
                      <button
                        type="button"
                        onClick={(e) => {
                          e.preventDefault();
                          e.stopPropagation();
                          setBadge(0);
                          dispatchNotifCleared();
                        }}
                        className="px-2 py-0.5 text-[11px] rounded border border-border text-fg-dim hover:text-fg hover:bg-bg-elev-1"
                        title="Reset the badge to zero"
                      >
                        Mark all read
                      </button>
                      <label className="flex items-center gap-1.5 text-[11px] text-fg-dim cursor-pointer">
                        <input
                          type="checkbox"
                          checked={settings.enabled}
                          onChange={(e) => update({ enabled: e.target.checked })}
                        />
                        Enabled
                      </label>
                    </div>
                  </div>
                  <div className="grid grid-cols-[1fr_auto_auto] gap-x-2 gap-y-1 text-[11px]">
                    <span className="text-fg-dim">Type</span>
                    <span className="text-fg-dim text-center w-12" title="Show toast">Toast</span>
                    <span className="text-fg-dim text-center w-12" title="Bubble emphasis">Bubble</span>
                    {NOTIF_TYPES.map((t) => (
                      <Fragment key={t.key}>
                        <span className="text-fg">{t.icon} {t.label}</span>
                        <input
                          type="checkbox"
                          className="justify-self-center"
                          checked={settings.toast[t.key]}
                          onChange={(e) => setToast(t.key, e.target.checked)}
                          disabled={!settings.enabled}
                        />
                        <input
                          type="checkbox"
                          className="justify-self-center"
                          checked={settings.bubble[t.key]}
                          onChange={(e) => setBubble(t.key, e.target.checked)}
                          disabled={!settings.enabled || !settings.toast[t.key]}
                          title="Larger sticker + coloured ring"
                        />
                      </Fragment>
                    ))}
                  </div>
                  <div className="mt-2 flex items-center justify-between gap-2">
                    <label className="flex items-center gap-1.5 text-[11px] text-fg-dim cursor-pointer">
                      <input
                        type="checkbox"
                        checked={settings.osPing}
                        onChange={(e) => update({ osPing: e.target.checked })}
                        disabled={!settings.enabled}
                      />
                      OS notification when tab is unfocused
                    </label>
                    <button
                      type="button"
                      onMouseDown={(e) => { e.stopPropagation(); }}
                      onClick={async (e) => {
                        e.preventDefault();
                        e.stopPropagation();
                        if (typeof Notification === "undefined") {
                          setOsTestMsg("✗ This browser has no Notification API.");
                          return;
                        }
                        let perm = Notification.permission;
                        setOsTestMsg(`Permission: ${perm}…`);
                        if (perm === "default") {
                          try {
                            perm = await Notification.requestPermission();
                          } catch {
                            setOsTestMsg("✗ requestPermission threw");
                            return;
                          }
                          setOsTestMsg(`Permission: ${perm}…`);
                        }
                        if (perm !== "granted") {
                          setOsTestMsg(
                            `✗ Permission ${perm}. Open Opera → site settings → allow Notifications.`,
                          );
                          return;
                        }
                        try {
                          const n = new Notification("Fastt test", {
                            body: "If you see this in your OS tray, it works.",
                            tag: "chatterly-test",
                          });
                          n.onshow = () => setOsTestMsg("✓ Fired — check your OS tray.");
                          n.onerror = () => setOsTestMsg("✗ Notification.onerror fired.");
                          setOsTestMsg("✓ Fired — check your OS tray.");
                        } catch (err) {
                          setOsTestMsg("✗ new Notification threw: " + (err as Error).message);
                        }
                      }}
                      className="px-2 py-0.5 text-[11px] rounded border border-border text-fg-dim hover:text-fg hover:bg-bg-elev-1"
                      title="Verify OS notifications are working"
                    >
                      Test
                    </button>
                  </div>
                  {osTestMsg && (
                    <div className="mt-1.5 text-[10px] text-fg-dim leading-snug">
                      {osTestMsg}
                    </div>
                  )}
                </div>
              )}
              <div className="flex-1 overflow-y-auto">
                {anyLoading && (
                  <div className="p-4 text-xs text-fg-dim">Loading…</div>
                )}
                {firstError && !anyLoading && (
                  <div className="p-4 text-xs text-err">
                    {firstError.message || "Failed to load notifications."}
                  </div>
                )}
                {!anyLoading && merged.length === 0 && !firstError && (
                  <div className="p-4 text-xs text-fg-dim">No notifications.</div>
                )}
                {merged.map((m, i) => (
                  <Row
                    key={`${m.accountId}:${m.n.id ?? i}`}
                    n={m.n}
                    accountId={m.accountId}
                    accountLabel={showAccountTag ? (accountLabel.get(m.accountId) ?? null) : null}
                  />
                ))}
              </div>
            </>
          )}
        </div>
      )}
    </div>
  );
}

function Row({
  n, accountId, accountLabel,
}: {
  n: NotificationItem;
  accountId: string;
  accountLabel: string | null;
}) {
  const user = n.user || n.fromUser || {};
  const avatar = proxyImage(user.avatar ?? null, accountId);
  const text = renderText(n.text || n.description || "", n.replacePairs);
  const href = user.id
    ? `/chat/${encodeURIComponent(accountId)}/${user.id}`
    : user.username
      ? `/chat/${encodeURIComponent(accountId)}/u/${encodeURIComponent(user.username)}`
      : null;
  const body = (
    <>
      <div className="w-7 h-7 rounded-full bg-bg-elev-1 overflow-hidden shrink-0 grid place-items-center">
        {avatar ? (
          <img src={avatar} alt="" loading="lazy" decoding="async" className="w-full h-full object-cover" />
        ) : (
          <span className="text-[10px] text-fg-dim">
            {(user.name || user.username || "?").slice(0, 1).toUpperCase()}
          </span>
        )}
      </div>
      <div className="min-w-0 flex-1">
        <div className="text-[12px] text-fg leading-snug">
          {user.name && <span className="font-medium">{user.name} </span>}
          <span className="text-fg-dim">{text}</span>
        </div>
        <div className="text-[10px] text-fg-dim mt-0.5 flex items-center gap-1.5">
          <span>{fmtTime(n.createdAt)}</span>
          {accountLabel && (
            <>
              <span>·</span>
              <span className="px-1 rounded bg-bg-elev-1 border border-border">{accountLabel}</span>
            </>
          )}
        </div>
      </div>
    </>
  );
  const cls = "px-3 py-2 border-b border-border/60 last:border-b-0 flex items-start gap-2.5 hover:bg-bg-elev-1/40";
  if (href) {
    return (
      <a
        href={href}
        target="_blank"
        rel="noreferrer"
        className={cls}
        onClick={() => dispatchNotifCleared()}
      >
        {body}
      </a>
    );
  }
  return <div className={cls}>{body}</div>;
}

function renderText(raw: string, replacePairs?: Record<string, string>): string {
  let s = raw;
  if (replacePairs) {
    for (const [k, v] of Object.entries(replacePairs)) {
      s = s.split(k).join(v);
    }
  }
  return s.replace(/<[^>]+>/g, "").replace(/\s+/g, " ").trim();
}

function fmtTime(iso?: string): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const ms = Date.now() - d.getTime();
  const sec = Math.floor(ms / 1000);
  if (sec < 60) return `${sec}s ago`;
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min}m ago`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr}h ago`;
  const day = Math.floor(hr / 24);
  if (day < 30) return `${day}d ago`;
  return d.toLocaleDateString([], { month: "short", day: "numeric" });
}

function useAccountLabelMap(accounts: ReturnType<typeof useActiveAccounts>): Map<string, string> {
  return useMemo(() => {
    const m = new Map<string, string>();
    for (const a of accounts) m.set(a.id, a.nickname || a.id);
    return m;
  }, [accounts]);
}
