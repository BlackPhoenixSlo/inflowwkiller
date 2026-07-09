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
import { useOFUser } from "@/hooks/useOFUser";
import { useMassExclude } from "@/hooks/useMassExclude";
import { useAllChatFolders, type ChatFolder } from "@/hooks/useChatFolders";

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

  // NOTE: the OF "Hide chat" action was removed from this menu (#9) — hiding a
  // chat on OF is not durable (the thread reappears on the next inbound), so the
  // button just confused operators. The relay endpoint (POST/DELETE
  // /api/of/v2/chats/{id}/hide) is left in place but is now unused from the UI.

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
  // 'of_restricted' = this user is restricted ON ONLYFANS via our UI — a
  // strictly stronger block (OF stops delivering their messages, automations
  // hard-skip, unread counts exclude). Surface it read-only here like the
  // muted case: flipping it to manual_restrict would silently drop the fan
  // out of the count-exclusion registry. Lift it via the OF-restrict row below.
  const ofRestrictSkip = restrictReason === "of_restricted";
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

  // Mass-exclude toggles: per-fan opt-out from mass PPV (MASSppvEXCLUDE) and/or
  // mass DM (MASSdmEXCLUDE) — two separate system lists so a fan can skip one
  // kind of broadcast without the other. PPV gates PRICED mass sends (mass PPV /
  // premade PPV); DM gates UNPRICED ones (mass DM / online blast / nudge). Each
  // mirrors a local kind='exclude' List to a same-named pinned OF list. Leaves
  // welcomes, AI replies and manual 1:1s intact — narrower than "Restrict from
  // automations" above. account_id + fan_id + kind ride the query string (the
  // /admin/do-not-mass handler reads them via Query, not the header).
  const ppvExclude = useMassExclude(accountId, fanId, "ppv");
  const dmExclude = useMassExclude(accountId, fanId, "dm");

  // ── Native OnlyFans restrict — DISTINCT from "Restrict from automations"
  // above. That one is an internal skip_list (only stops OUR automations);
  // this is OF's own shadow-restrict: the fan can still type, but OF stops
  // delivering their messages to us (isRestricted→true, canReceiveChatMessage
  // →false). Restricting through the relay also (1) sends one "." first +
  // marks the chat read, so the thread leaves the unread/owe-reply state
  // instead of sitting flagged forever, and (2) writes the durable
  // `of_restricted` registry that keeps the creator OUT of the roster/inbox
  // unread counts and hard-skipped by every automation. Live state comes off
  // the OF user object OR the relay's row stamp (covers the thin /chats rows).
  const ofUser = useOFUser(accountId, fanId);
  // Warm the account's custom-list cache now so the "Add to list" submenu opens
  // instantly (shared query key with the submenu; 60s cached).
  useAllChatFolders(accountId);
  const isRestricted =
    !!ofUser.data?.isRestricted || !!chat.withUser?.isRestricted || ofRestrictSkip;

  const toggleOfRestrict = useMutation({
    mutationFn: async () => {
      if (isRestricted) {
        await relay.delete(`/api/of/v2/users/${fanId}/restrict`, { accountId });
        return null;
      }
      return relay.post<{ acked?: boolean }>(
        `/api/of/v2/users/${fanId}/restrict`, undefined, { accountId });
    },
    onSuccess: (data) => {
      // Refetch the OF user so the label flips to its true post-toggle state;
      // refresh the inbox + badges + this thread too — the relay's ack flow
      // just changed lastMessage/read-state and the count-exclusion registry.
      qc.invalidateQueries({ queryKey: ["of-user", accountId, fanId] });
      qc.invalidateQueries({ queryKey: ["chats"] });
      qc.invalidateQueries({ queryKey: ["roster-counts"] });
      qc.invalidateQueries({ queryKey: ["messages", accountId, fanId] });
      qc.invalidateQueries({ queryKey: ["automation-restrict"] });
      qc.invalidateQueries({ queryKey: ["of-restricted-list", accountId] });
      flash(true, isRestricted
        ? "OnlyFans restrict lifted"
        : data?.acked
          ? "Sent “.” · restricted on OnlyFans"
          : "Restricted on OnlyFans");
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
          {autoMuted || ofRestrictSkip ? (
            <Item
              label={autoMuted ? "Auto-restricted (chat muted)" : "Auto-restricted (OF restrict)"}
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
          <Item
            label={ppvExclude.isOn ? "Remove from MASSppvEXCLUDE" : "Add to MASSppvEXCLUDE"}
            disabled={ppvExclude.toggle.isPending || ppvExclude.isLoading}
            onClick={() =>
              ppvExclude.toggle.mutate(!ppvExclude.isOn, {
                onSuccess: (data, next) =>
                  flash(true,
                    (next ? "Added to MASSppvEXCLUDE" : "Mass PPVs allowed") +
                    (data?.of_synced === false ? " (OF sync pending)" : "")),
                onError: (e: Error) => flash(false, e.message),
              })
            }
            icon={ppvExclude.isOn ? "✅" : "🚫"}
          />
          <Item
            label={dmExclude.isOn ? "Remove from MASSdmEXCLUDE" : "Add to MASSdmEXCLUDE"}
            disabled={dmExclude.toggle.isPending || dmExclude.isLoading}
            onClick={() =>
              dmExclude.toggle.mutate(!dmExclude.isOn, {
                onSuccess: (data, next) =>
                  flash(true,
                    (next ? "Added to MASSdmEXCLUDE" : "Mass DMs allowed") +
                    (data?.of_synced === false ? " (OF sync pending)" : "")),
                onError: (e: Error) => flash(false, e.message),
              })
            }
            icon={dmExclude.isOn ? "✅" : "🚫"}
          />
          {/* Always offered (was gated on OF's canRestrict, which the thin
              creator rows often omit — the one chat type this action is FOR).
              OF itself rejects the rare user it truly can't restrict. */}
          <Item
            label={isRestricted ? "Lift OnlyFans restrict" : "Restrict on OnlyFans"}
            disabled={toggleOfRestrict.isPending}
            onClick={() => toggleOfRestrict.mutate()}
            icon={isRestricted ? "🔓" : "🛑"}
          />
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

function ToggleListsSubmenu({
  accountId, fanId, onFlash, onBack,
}: {
  accountId: string;
  fanId: number;
  onFlash: (msg: string, ok: boolean) => void;
  onBack: () => void;
}) {
  const qc = useQueryClient();
  // The lists an operator can add/remove a fan to come from /chats/folders
  // (custom + close-friends lists, canAddUsers=true) — NOT the OF /users
  // listsStates, which only returns SYSTEM buckets (fans/following/…) with
  // canAddUser=false. Reading listsStates is why this menu used to say "No
  // lists you can modify" even on accounts with dozens of custom lists.
  const foldersQ = useAllChatFolders(accountId);
  // In-flight list ids so we disable just the row being toggled.
  const [busy, setBusy] = useState<Set<string>>(new Set());
  // Optimistic membership overrides — OF's users[] preview is capped so we
  // can't always know membership up front; flip it locally on click.
  const [override, setOverride] = useState<Map<string, boolean>>(new Map());

  const lists = (foldersQ.data ?? []).filter((f) => f.canAddUsers);

  function isMember(f: ChatFolder): boolean {
    const idStr = String(f.id);
    if (override.has(idStr)) return !!override.get(idStr);
    return (f.users ?? []).some((u) => Number(u.id) === fanId);
  }

  async function toggle(f: ChatFolder) {
    const idStr = String(f.id);
    const currentlyIn = isMember(f);
    setBusy((prev) => new Set(prev).add(idStr));
    try {
      if (currentlyIn) {
        await relay.delete(
          `/api/of/v2/lists/${encodeURIComponent(idStr)}/users/${fanId}`,
          { accountId },
        );
        onFlash(`Removed from "${f.name || idStr}"`, true);
      } else {
        await relay.post(
          `/api/of/v2/lists/${encodeURIComponent(idStr)}/users/${fanId}`,
          undefined,
          { accountId },
        );
        onFlash(`Added to "${f.name || idStr}"`, true);
      }
      setOverride((prev) => new Map(prev).set(idStr, !currentlyIn));
      // Membership changed → refresh the folder chips/counts + roster so the
      // VIP/list view reflects it immediately.
      qc.invalidateQueries({ queryKey: ["chat-folders", accountId] });
      qc.invalidateQueries({ queryKey: ["chats"] });
    } catch (e) {
      onFlash((e as Error).message || "Couldn't update that list", false);
    } finally {
      setBusy((prev) => {
        const next = new Set(prev);
        next.delete(idStr);
        return next;
      });
    }
  }

  return (
    <div className="max-h-[280px] overflow-y-auto">
      <button
        type="button"
        onClick={onBack}
        className="w-full text-left px-3 py-1.5 text-[11px] text-fg-dim hover:text-fg hover:bg-bg-elev-1 border-b border-border"
      >
        ← Back
      </button>
      {foldersQ.isLoading && (
        <div className="px-3 py-2 text-[11px] text-fg-dim">Loading lists…</div>
      )}
      {foldersQ.isError && (
        <div className="px-3 py-2 text-[11px] text-err">
          {(foldersQ.error as Error)?.message || "Failed to load lists"}
        </div>
      )}
      {!foldersQ.isLoading && !foldersQ.isError && lists.length === 0 && (
        <div className="px-3 py-2 text-[11px] text-fg-dim">
          No custom lists on this account yet. Create one on OF first.
        </div>
      )}
      {lists.map((f) => {
        const idStr = String(f.id);
        const inList = isMember(f);
        const inflight = busy.has(idStr);
        return (
          <button
            key={idStr}
            type="button"
            disabled={inflight}
            onClick={() => toggle(f)}
            className="w-full flex items-center gap-2 px-3 py-1.5 text-xs text-left text-fg hover:bg-bg-elev-1 disabled:opacity-60 disabled:hover:bg-transparent"
            title={inList ? "Remove from list" : "Add to list"}
          >
            <span
              className={
                "w-4 h-4 rounded border grid place-items-center text-[10px] shrink-0 " +
                (inList ? "bg-accent border-accent text-white" : "border-border bg-bg")
              }
            >
              {inList ? "✓" : ""}
            </span>
            <span className="flex-1 truncate">{f.name || `list ${idStr}`}</span>
            {typeof f.usersCount === "number" && (
              <span className="text-[9px] text-fg-dim">{f.usersCount}</span>
            )}
            {inflight && <span className="text-[9px] text-fg-dim">…</span>}
          </button>
        );
      })}
    </div>
  );
}
