"use client";

/**
 * ChatActionsMenu — overflow popover in the chat header.
 *
 * Hosts the four /ui/ parity actions OF web has on every chat:
 *   • Mark as unread  → DELETE /chats/{id}/mark-as-read (server-side)
 *   • Mute / Unmute   → POST/DELETE /chats/{id}/mute
 *   • Hide / Unhide   → POST/DELETE /chats/{id}/hide
 *   • Add to list ▸   → sub-menu over `/chats/folders` (all custom lists),
 *                       each line is a POST /lists/{id}/users/{fanId}
 *
 * Closes on outside-click + Esc. Each action shows a transient status
 * line at the bottom (✓ done · err msg). The inbox cache is patched
 * locally on mark-unread so the blue dot reappears instantly, and the
 * chat-folders cache stays untouched because membership doesn't affect
 * the visible folder list — only the per-fan membership state, which
 * the inbox doesn't display.
 */

import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { relay, type OFChatItem } from "@/lib/relay";
import { useOFUser, type OFListState } from "@/hooks/useOFUser";

interface Props {
  accountId: string;
  fanId: number;
  chat: OFChatItem;
  onClosed: () => void;
}

export function ChatActionsMenu({ accountId, fanId, chat, onClosed }: Props) {
  const qc = useQueryClient();
  const [submenu, setSubmenu] = useState<null | "list">(null);
  const [status, setStatus] = useState<{ ok: boolean; msg: string } | null>(null);
  const ref = useRef<HTMLDivElement | null>(null);

  // Local mirror of mute state. OF's response for the toggle doesn't
  // include the new state, so we flip optimistically and reconcile on
  // the next chat-list refresh.
  const [muted, setMuted] = useState(!!chat.isMutedNotifications);

  useEffect(() => { setMuted(!!chat.isMutedNotifications); }, [chat.isMutedNotifications]);

  useEffect(() => {
    const onClick = (e: MouseEvent) => {
      if (!ref.current?.contains(e.target as Node)) onClosed();
    };
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClosed(); };
    document.addEventListener("mousedown", onClick);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onClick);
      document.removeEventListener("keydown", onKey);
    };
  }, [onClosed]);

  function flash(ok: boolean, msg: string) {
    setStatus({ ok, msg });
    setTimeout(() => setStatus(null), 2500);
  }

  const markUnread = useMutation({
    mutationFn: () =>
      relay.delete(`/api/of/v2/chats/${fanId}/mark-as-read`, { accountId }),
    onSuccess: () => {
      // Patch chats cache: bump the row's hasUnread + count back so the
      // blue dot returns in the list immediately.
      type Page = { rows: OFChatItem[]; hasMore: boolean };
      type Infinite = { pages: Page[]; pageParams: unknown[] };
      qc.getQueryCache().findAll({ queryKey: ["chats"] }).forEach((q) => {
        const data = q.state.data as Infinite | undefined;
        if (!data?.pages) return;
        const newPages: Page[] = data.pages.map((p) => ({
          ...p,
          rows: p.rows.map((c) =>
            (c.__accountId ?? "") === accountId && c.withUser.id === fanId
              ? { ...c, hasUnread: true, unreadMessagesCount: Math.max(1, c.unreadMessagesCount ?? 1) }
              : c,
          ),
        }));
        qc.setQueryData(q.queryKey, { ...data, pages: newPages });
      });
      flash(true, "Marked unread");
    },
    onError: (e: Error) => flash(false, e.message),
  });

  const toggleMute = useMutation({
    mutationFn: () =>
      muted
        ? relay.delete(`/api/of/v2/chats/${fanId}/mute`, { accountId })
        : relay.post(`/api/of/v2/chats/${fanId}/mute`, undefined, { accountId }),
    onSuccess: () => {
      setMuted((v) => !v);
      flash(true, muted ? "Notifications unmuted" : "Notifications muted");
    },
    onError: (e: Error) => flash(false, e.message),
  });

  const hide = useMutation({
    mutationFn: () =>
      relay.post(`/api/of/v2/chats/${fanId}/hide`, undefined, { accountId }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["chats"] });
      flash(true, "Chat hidden");
    },
    onError: (e: Error) => flash(false, e.message),
  });

  // "Restrict from automations" — a durable skip_list row in OUR DB (not OF), so
  // NO automation (welcome / AI chat / nudge / tip reward / …) ever messages this
  // fan again until it's lifted. `reason==='muted_creator'` means it was set
  // automatically because the chat is muted (an OF mute) — unmuting (above) lifts
  // that, so we surface it read-only rather than offer a manual toggle.
  const restrictQ = useQuery({
    queryKey: ["automation-restrict", accountId, fanId],
    queryFn: () =>
      relay.get<{ restricted: boolean; reason: string | null }>(
        `/api/of/v2/automation-restrict/${fanId}`, { accountId }),
    staleTime: 30_000,
  });
  const restrictReason = restrictQ.data?.reason ?? null;
  const autoMuted = restrictReason === "muted_creator";
  const [manualRestricted, setManualRestricted] = useState(false);
  useEffect(() => {
    setManualRestricted(restrictReason === "manual_restrict");
  }, [restrictReason]);

  const toggleRestrict = useMutation({
    mutationFn: () =>
      manualRestricted
        ? relay.delete(`/api/of/v2/automation-restrict/${fanId}`, { accountId })
        : relay.post(`/api/of/v2/automation-restrict/${fanId}`, undefined, { accountId }),
    onSuccess: () => {
      setManualRestricted((v) => !v);
      qc.invalidateQueries({ queryKey: ["automation-restrict"] });
      flash(true, manualRestricted ? "Automations resumed" : "Restricted from automations");
    },
    onError: (e: Error) => flash(false, e.message),
  });

  // ── Native OnlyFans restrict — DISTINCT from "Restrict from automations"
  // above. That one is an internal skip_list (only stops OUR automations);
  // this is OF's own shadow-restrict: the fan can still type, but OF stops
  // delivering their messages to us (isRestricted→true, canReceiveChatMessage
  // →false). The live state comes off the OF user object — the same query
  // backs the "Add to list" submenu + the drawer, so React Query dedupes it.
  const ofUser = useOFUser(accountId, fanId);
  const isRestricted = !!ofUser.data?.isRestricted;
  const canRestrict = !!ofUser.data?.canRestrict;

  const toggleOfRestrict = useMutation({
    mutationFn: () =>
      isRestricted
        ? relay.delete(`/api/of/v2/users/${fanId}/restrict`, { accountId })
        : relay.post(`/api/of/v2/users/${fanId}/restrict`, undefined, { accountId }),
    onSuccess: () => {
      // Refetch the OF user so the label flips to its true post-toggle state;
      // refresh the inbox too since a restricted fan usually drops out of the
      // active conversation set. Kept separate from `hide` so lifting the
      // restrict never force-unhides the chat.
      qc.invalidateQueries({ queryKey: ["of-user", accountId, fanId] });
      qc.invalidateQueries({ queryKey: ["chats"] });
      flash(true, isRestricted ? "OnlyFans restrict lifted" : "Restricted on OnlyFans");
    },
    onError: (e: Error) => flash(false, e.message),
  });

  return (
    <div
      ref={ref}
      className="absolute right-2 top-full mt-1 z-40 min-w-[220px] bg-panel border border-border rounded-lg shadow-xl py-1"
    >
      {!submenu && (
        <>
          <Item
            label="Mark as unread"
            disabled={markUnread.isPending}
            onClick={() => markUnread.mutate()}
            icon="●"
          />
          <Item
            label={muted ? "Unmute notifications" : "Mute notifications"}
            disabled={toggleMute.isPending}
            onClick={() => toggleMute.mutate()}
            icon={muted ? "🔔" : "🔕"}
          />
          <Item
            label="Hide chat"
            disabled={hide.isPending}
            onClick={() => hide.mutate()}
            icon="🚫"
          />
          {autoMuted ? (
            <Item
              label="Auto-restricted (chat muted)"
              disabled
              onClick={() => {}}
              icon="🔕"
            />
          ) : (
            <Item
              label={manualRestricted ? "Allow automations" : "Restrict from automations"}
              disabled={toggleRestrict.isPending || restrictQ.isLoading}
              onClick={() => toggleRestrict.mutate()}
              icon={manualRestricted ? "▶" : "⛔"}
            />
          )}
          {(isRestricted || canRestrict) && (
            <Item
              label={isRestricted ? "Lift OnlyFans restrict" : "Restrict on OnlyFans"}
              disabled={toggleOfRestrict.isPending || ofUser.isLoading}
              onClick={() => toggleOfRestrict.mutate()}
              icon={isRestricted ? "🔓" : "🛑"}
            />
          )}
          <Item
            label="Add to list ▸"
            onClick={() => setSubmenu("list")}
            icon="📂"
          />
        </>
      )}
      {submenu === "list" && (
        <ToggleListsSubmenu
          accountId={accountId}
          fanId={fanId}
          onFlash={(msg, ok) => flash(ok, msg)}
          onBack={() => setSubmenu(null)}
        />
      )}
      {status && (
        <div
          className={
            "px-3 py-1 text-[11px] border-t border-border " +
            (status.ok ? "text-ok" : "text-err")
          }
        >
          {status.ok ? "✓ " : "✗ "}{status.msg}
        </div>
      )}
    </div>
  );
}

function Item({
  label, onClick, disabled, icon,
}: { label: string; onClick: () => void; disabled?: boolean; icon?: string }) {
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={onClick}
      className="w-full flex items-center gap-2 px-3 py-1.5 text-xs text-left text-fg hover:bg-bg-elev-1 disabled:opacity-50"
    >
      <span className="w-4 text-center text-[11px]">{icon}</span>
      <span>{label}</span>
    </button>
  );
}

/** Lists you can never modify — even if hasUser is true. We still show
 *  them as ✓ chips (so the user knows the membership) but disable the
 *  toggle button. `recent` and `tagged` are OF-system-managed entirely. */
const SYSTEM_LOCKED = new Set(["fans", "following", "recent", "tagged", "rebill_on", "rebill_off"]);

function ToggleListsSubmenu({
  accountId, fanId, onFlash, onBack,
}: {
  accountId: string;
  fanId: number;
  onFlash: (msg: string, ok: boolean) => void;
  onBack: () => void;
}) {
  const qc = useQueryClient();
  const ofUser = useOFUser(accountId, fanId);
  // Track in-flight list ids so we can disable just the row being toggled,
  // not the whole submenu.
  const [busy, setBusy] = useState<Set<string>>(new Set());

  // OF returns membership inline in /users/{id}.listsStates. We mirror it
  // into local state so each click flips the visible ✓ instantly; the
  // real source of truth gets refreshed on the next of-user refetch.
  const [local, setLocal] = useState<Map<string, boolean> | null>(null);
  useEffect(() => {
    const states = ofUser.data?.listsStates ?? [];
    const m = new Map<string, boolean>();
    for (const s of states) m.set(String(s.id), !!s.hasUser);
    setLocal(m);
  }, [ofUser.data]);

  async function toggle(s: OFListState) {
    const idStr = String(s.id);
    const currentlyIn = local?.get(idStr) ?? !!s.hasUser;
    setBusy((prev) => new Set(prev).add(idStr));
    try {
      if (currentlyIn) {
        await relay.delete(
          `/api/of/v2/lists/${encodeURIComponent(idStr)}/users/${fanId}`,
          { accountId },
        );
        onFlash(`Removed from "${s.name || idStr}"`, true);
      } else {
        await relay.post(
          `/api/of/v2/lists/${encodeURIComponent(idStr)}/users/${fanId}`,
          undefined,
          { accountId },
        );
        onFlash(`Added to "${s.name || idStr}"`, true);
      }
      setLocal((prev) => {
        const next = new Map(prev ?? []);
        next.set(idStr, !currentlyIn);
        return next;
      });
      // Background refresh so the next open sees authoritative server state.
      qc.invalidateQueries({ queryKey: ["of-user", accountId, fanId] });
    } catch (e) {
      onFlash((e as Error).message || "Toggle failed", false);
    } finally {
      setBusy((prev) => {
        const next = new Set(prev);
        next.delete(idStr);
        return next;
      });
    }
  }

  const visible = (ofUser.data?.listsStates ?? []).filter((s) => {
    // Show: any list the fan is already in (so removal is possible) OR
    // any list the user can add to. Skip "recent"/"tagged" which OF
    // updates implicitly — keeping them would be confusing.
    if (s.type === "recent" || s.type === "tagged") return false;
    return s.hasUser || s.canAddUser;
  });

  return (
    <div className="max-h-[280px] overflow-y-auto">
      <button
        type="button"
        onClick={onBack}
        className="w-full text-left px-3 py-1.5 text-[11px] text-fg-dim hover:text-fg hover:bg-bg-elev-1 border-b border-border"
      >
        ← Back
      </button>
      {ofUser.isLoading && (
        <div className="px-3 py-2 text-[11px] text-fg-dim">Loading lists…</div>
      )}
      {ofUser.isError && (
        <div className="px-3 py-2 text-[11px] text-err">
          {(ofUser.error as Error)?.message || "Failed to load lists"}
        </div>
      )}
      {!ofUser.isLoading && visible.length === 0 && (
        <div className="px-3 py-2 text-[11px] text-fg-dim">
          No lists you can modify. Create one on OF first.
        </div>
      )}
      {visible.map((s) => {
        const idStr = String(s.id);
        const inList = local?.get(idStr) ?? !!s.hasUser;
        const locked = SYSTEM_LOCKED.has(s.type) && !s.canAddUser;
        const inflight = busy.has(idStr);
        return (
          <button
            key={idStr}
            type="button"
            disabled={inflight || locked}
            onClick={() => toggle(s)}
            className="w-full flex items-center gap-2 px-3 py-1.5 text-xs text-left text-fg hover:bg-bg-elev-1 disabled:opacity-60 disabled:hover:bg-transparent"
            title={locked ? "System-managed, can't be modified" : (inList ? "Remove" : "Add")}
          >
            <span
              className={
                "w-4 h-4 rounded border grid place-items-center text-[10px] shrink-0 " +
                (inList
                  ? "bg-accent border-accent text-white"
                  : "border-border bg-bg")
              }
            >
              {inList ? "✓" : ""}
            </span>
            <span className="flex-1 truncate">{s.name || `list ${idStr}`}</span>
            {locked && <span className="text-[9px] text-fg-dim">system</span>}
            {inflight && <span className="text-[9px] text-fg-dim">…</span>}
          </button>
        );
      })}
    </div>
  );
}
