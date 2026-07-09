"use client";

/**
 * MoneyRail — sticky bottom-right dock for money notifications (purchases
 * + tips) so chatters can jump straight to the fans who just paid.
 *
 * Two states, both persisted in localStorage so a reload / popout comes
 * back exactly how you left it:
 *   • collapsed — compact card showing the last 3 money events straight
 *     from the localStorage notif-history mirror (zero network).
 *   • expanded  — taller list (~8 rows visible, scroll for up to 50) that
 *     additionally fetches OF's /notifications?type=purchases|tip for each
 *     in-scope account. Uses the SAME ["notif-list", aid, type] query keys
 *     as the bell, so the cache is shared and the toaster's invalidation
 *     on SSE arrival refreshes this list too.
 *
 * Sticky by design: it never closes on outside click or Escape — only
 * tapping the header toggles it. Rows deep-link to the fan's chat with
 * the one-shot ?refresh=media flag (same as money toasts) since the fan
 * just moved money and the popout's persisted caches are stale by seconds.
 */

import { useEffect, useMemo, useState } from "react";
import { useQueries } from "@tanstack/react-query";

import { useScope } from "@/contexts/ScopeContext";
import { useActiveAccounts } from "@/hooks/useAccounts";
import { relay, proxyImage } from "@/lib/relay";
import {
  mapOfTypeToKey,
  readNotifHistory,
  type CachedNotification,
  type NotifTypeKey,
} from "@/lib/notifSettings";

const MONEY_TYPES = ["purchases", "tip"] as const;

const STATE_KEY = "chatterly:money-rail:v1";
const COLLAPSED_ROWS = 3;
const MAX_ROWS = 50;

function readOpen(): boolean {
  if (typeof window === "undefined") return false;
  try {
    const raw = window.localStorage.getItem(STATE_KEY);
    if (!raw) return false;
    return (JSON.parse(raw) as { open?: boolean }).open === true;
  } catch {
    return false;
  }
}

function writeOpen(open: boolean): void {
  try { window.localStorage.setItem(STATE_KEY, JSON.stringify({ open })); } catch { /* ignore */ }
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
  [k: string]: unknown;
}

interface MergedItem {
  n: NotificationItem;
  accountId: string;
  typeKey: NotifTypeKey;
  ts: number;
}

function notifPath(type: string): string {
  const q = new URLSearchParams({ limit: "20", offset: "0", type });
  return `/api/of/v2/users/notifications?${q.toString()}`;
}

function parseList(raw: unknown): NotificationItem[] {
  if (Array.isArray(raw)) return raw as NotificationItem[];
  if (raw && typeof raw === "object" && Array.isArray((raw as { list?: unknown }).list)) {
    return (raw as { list: NotificationItem[] }).list;
  }
  return [];
}

export function MoneyRail() {
  const { accountId } = useScope();
  const activeAccounts = useActiveAccounts();
  const [open, setOpen] = useState<boolean>(() => readOpen());
  // Render nothing until after hydration — the collapsed list comes from
  // localStorage, which the server can't see.
  const [mounted, setMounted] = useState(false);
  useEffect(() => { setMounted(true); }, []);

  const toggle = () => {
    setOpen((v) => {
      writeOpen(!v);
      return !v;
    });
  };

  const targetAccountIds = useMemo<string[]>(
    () => (accountId ? [accountId] : activeAccounts.map((a) => a.id)),
    [accountId, activeAccounts],
  );

  // Re-render whenever the SSE toaster mirrors a new arrival into the
  // localStorage history (same-tab custom event + cross-tab storage event).
  const [historyTick, bumpHistory] = useState(0);
  useEffect(() => {
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
  }, []);

  // OF fan-out — one query per account × money type, enabled only while
  // expanded. Query keys intentionally match the bell's so both share one
  // cache entry per (account, type).
  const queryDefs = useMemo(
    () => targetAccountIds.flatMap((aid) => MONEY_TYPES.map((type) => ({ aid, type }))),
    [targetAccountIds],
  );
  const listQueries = useQueries({
    queries: queryDefs.map(({ aid, type }) => ({
      queryKey: ["notif-list", aid, type] as const,
      queryFn: async () => parseList(await relay.get(notifPath(type), { accountId: aid })),
      enabled: open,
      staleTime: 30_000,
      refetchOnWindowFocus: false,
    })),
  });

  const dataSig = listQueries.map((q) => q.dataUpdatedAt).join(",");

  const merged = useMemo<MergedItem[]>(() => {
    const acc: MergedItem[] = [];
    const seenIds = new Set<string>();
    listQueries.forEach((q, i) => {
      const def = queryDefs[i];
      if (!def) return;
      for (const n of q.data ?? []) {
        const ts = n.createdAt ? Date.parse(n.createdAt) || 0 : 0;
        if (n.id != null) seenIds.add(`${def.aid}:${n.id}`);
        acc.push({ n, accountId: def.aid, typeKey: def.type, ts });
      }
    });
    const history = readNotifHistory();
    for (const aid of targetAccountIds) {
      const items: CachedNotification[] = history[aid] ?? [];
      for (const c of items) {
        const key = c.typeKey ?? mapOfTypeToKey(c.type);
        if (key !== "tip" && key !== "purchases") continue;
        if (seenIds.has(`${aid}:${c.id}`)) continue;
        const ts = c.createdAt ? Date.parse(c.createdAt) || 0 : 0;
        acc.push({
          n: {
            id: c.id,
            type: c.type,
            text: c.text,
            createdAt: c.createdAt,
            user: c.user,
            replacePairs: c.replacePairs,
          },
          accountId: aid,
          typeKey: key,
          ts,
        });
      }
    }
    acc.sort((a, b) => b.ts - a.ts);
    return acc.slice(0, MAX_ROWS);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dataSig, queryDefs, targetAccountIds, historyTick]);

  const anyLoading = open && listQueries.some((q) => q.isLoading);
  const showAccountTag = targetAccountIds.length > 1;
  const accountLabel = useMemo(() => {
    const m = new Map<string, string>();
    for (const a of activeAccounts) m.set(a.id, a.nickname || a.id);
    return m;
  }, [activeAccounts]);

  if (!mounted || targetAccountIds.length === 0) return null;

  const visible = open ? merged : merged.slice(0, COLLAPSED_ROWS);

  return (
    <div className="fixed bottom-3 right-3 z-40 hidden md:flex w-[290px] flex-col bg-panel border border-border rounded-lg shadow-xl overflow-hidden">
      <button
        type="button"
        onClick={toggle}
        className="flex items-center justify-between gap-2 px-3 py-2 bg-bg-elev-1/50 hover:bg-bg-elev-1 border-b border-border text-left"
        title={open ? "Collapse" : "Expand"}
        aria-expanded={open}
      >
        <span className="text-[12px] font-medium text-fg flex items-center gap-1.5">
          <span aria-hidden>💰</span> Buys &amp; tips
        </span>
        <span className="text-[11px] text-fg-dim">{open ? "▾ close" : "▴"}</span>
      </button>
      <div className={open ? "max-h-[420px] overflow-y-auto" : "overflow-hidden"}>
        {anyLoading && merged.length === 0 && (
          <div className="px-3 py-2.5 text-[11px] text-fg-dim">Loading…</div>
        )}
        {visible.length === 0 && !anyLoading && (
          <div className="px-3 py-2.5 text-[11px] text-fg-dim">
            No recent purchases or tips.
          </div>
        )}
        {visible.map((m, i) => (
          <Row
            key={`${m.accountId}:${m.n.id ?? i}`}
            m={m}
            accountLabel={showAccountTag ? (accountLabel.get(m.accountId) ?? null) : null}
          />
        ))}
      </div>
    </div>
  );
}

function Row({ m, accountLabel }: { m: MergedItem; accountLabel: string | null }) {
  const { n, accountId, typeKey } = m;
  const user = n.user || n.fromUser || {};
  const avatar = proxyImage(user.avatar ?? null, accountId);
  const text = renderText(n.text || n.description || "", n.replacePairs);
  const base = user.id
    ? `/chat/${encodeURIComponent(accountId)}/${user.id}`
    : user.username
      ? `/chat/${encodeURIComponent(accountId)}/u/${encodeURIComponent(user.username)}`
      : null;
  const href = base ? `${base}${base.includes("?") ? "&" : "?"}refresh=media` : null;
  const body = (
    <>
      <div className="w-7 h-7 rounded-full bg-bg-elev-1 overflow-hidden shrink-0 grid place-items-center relative">
        {avatar ? (
          <img src={avatar} alt="" loading="lazy" decoding="async" className="w-full h-full object-cover" />
        ) : (
          <span className="text-[10px] text-fg-dim">
            {(user.name || user.username || "?").slice(0, 1).toUpperCase()}
          </span>
        )}
        <span className="absolute -bottom-1 -right-1 text-[10px]" aria-hidden>
          {typeKey === "tip" ? "💸" : "💰"}
        </span>
      </div>
      <div className="min-w-0 flex-1">
        <div className="text-[12px] text-fg leading-snug truncate">
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
      <a href={href} target="_blank" rel="noreferrer" className={cls}>
        {body}
      </a>
    );
  }
  return <div className={cls}>{body}</div>;
}

function renderText(raw: string, replacePairs?: Record<string, string>): string {
  let s = raw;
  if (replacePairs) {
    for (const [k, v] of Object.entries(replacePairs)) s = s.split(k).join(v);
  }
  return s.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();
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
