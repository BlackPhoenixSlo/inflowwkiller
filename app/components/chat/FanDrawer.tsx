"use client";

/**
 * FanDrawer — slide-over panel on the right edge of the chat surface.
 *
 * Opens when the user clicks the chat header. Renders the fan's identity
 * (display name, username, avatar) on top, then editable fields:
 *   • Custom nickname  — what our team calls them ("the German guy")
 *   • Notes            — free-form per-team memory
 *   • Tags             — comma-separated chips
 *
 * Lifetime spend + activity timestamps sit at the bottom (read-only;
 * computed from transactions/messages tables).
 *
 * Save: every field saves on blur (notes/nickname) or chip-edit (tags).
 * The mutation patches the cache directly so the panel feels instant.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { useFan } from "@/hooks/useFan";
import { useAccountLabel } from "@/hooks/useAccounts";
import { OF_USER_STALE_MS, useOFUser } from "@/hooks/useOFUser";
import { useMassExclude, type MassExcludeKind } from "@/hooks/useMassExclude";
import { useFanActivity } from "@/hooks/useLastPurchases";
import { useFanPpvHistory, type PpvHistoryItem } from "@/hooks/useFanPpvHistory";
import { useFanChatMedia, type FanChatMediaItem } from "@/hooks/useFanChatMedia";
import { stripOFHtml } from "@/lib/ofHtml";
import { relay, type FanRecord, type OFChatItem, type VaultMedia } from "@/lib/relay";
import { proxyImage, proxyScrubFrame } from "@/lib/mediaUrl";
import { type FanId } from "@/lib/fanId";
import { cn, fmtRelTime, interpretSubStatus } from "@/lib/utils";
import { PersonaClaims } from "./PersonaClaims";
import { type PickedTemplate } from "./TemplatePicker";
import { useAiChatterSessions, useCancelOffer } from "@/hooks/useCatalog";

/** 🎬 chip: this fan has an open AI-seller offer. The contract between the bot
 *  and human chatters — see it, and kill the offer to take over cleanly.
 *  Renders nothing when the fan isn't in any AI-seller flow. (The tip/both
 *  mode branches render LEGACY open rows — new offers are always ppv.) */
function AiSellerChip({ accountId, fanId }: { accountId: string; fanId: FanId }) {
  const sessionsQ = useAiChatterSessions(accountId);
  const cancelM = useCancelOffer(accountId);
  const offer = (sessionsQ.data?.offers ?? []).find(
    (o) => o.fan_id === fanId && o.status === "open",
  );
  if (!offer) return null;
  return (
    <div className="mt-1 inline-flex flex-wrap md:flex-nowrap items-center gap-1.5 rounded-full border border-amber-500/40 bg-amber-500/10 px-2 py-0.5 text-[10px] text-amber-400">
      <span>🎬</span>
      <span>
        waiting on{" "}
        {offer.mode === "both"
          ? `$${Math.round((offer.tip_unlock_cents || offer.price_cents) / 100)} unlock/tip`
          : offer.mode === "tip"
            ? `$${Math.round(offer.tip_unlock_cents / 100)} tip`
            : `$${Math.round(offer.price_cents / 100)} unlock`}
        {offer.tips_accum_cents > 0 &&
          ` (${Math.round(offer.tips_accum_cents / 100)} in)`}
      </span>
      <button
        type="button"
        title="Cancel the AI offer and take over"
        className="shrink-0 md:shrink grid md:block place-items-center w-9 h-9 md:w-auto md:h-auto -my-2 md:my-0 text-base md:text-[10px] text-amber-400/70 hover:text-red-400"
        onClick={() => cancelM.mutate(offer.id)}
      >
        ×
      </button>
    </div>
  );
}

/** 🚫 Mass-exclude toggle — flags a fan onto MASSppvEXCLUDE (kind="ppv") or
 *  MASSdmEXCLUDE (kind="dm") so that kind of mass send skips them. Each mirrors a
 *  server-side List(kind='exclude') to a same-named pinned OF list; PPV gates
 *  priced mass sends, DM gates unpriced ones (mass DM / online blast / nudge).
 *  Excluded fans STILL get welcomes / AI replies / manual 1:1s. Shares its query
 *  key with the chat-header ⋯ menu toggle so both stay in sync. Optimistic. */
function MassExcludeToggle({
  accountId, fanId, kind, title, hint,
}: {
  accountId: string; fanId: FanId; kind: MassExcludeKind; title: string; hint: string;
}) {
  const { isOn, isLoading, toggle } = useMassExclude(accountId, fanId, kind);
  return (
    <div className="flex items-center justify-between gap-3 rounded-md border border-border bg-bg px-2.5 py-2">
      <div className="min-w-0">
        <div className="text-xs font-medium text-fg flex items-center gap-1.5">
          <span>🚫</span> {title}
        </div>
        <div className="hidden md:block text-[10px] text-fg-dim leading-tight mt-0.5">{hint}</div>
      </div>
      <button
        type="button"
        role="switch"
        aria-checked={isOn}
        disabled={isLoading || toggle.isPending}
        onClick={() => toggle.mutate(!isOn)}
        title={isOn ? `Remove from ${title}` : `Add to ${title}`}
        className={cn(
          "relative shrink-0 w-12 h-7 md:w-10 md:h-6 rounded-full transition-colors disabled:opacity-50",
          isOn ? "bg-accent" : "bg-bg-elev-1 border border-border",
        )}
      >
        <span
          className={cn(
            "absolute top-0.5 left-0.5 w-6 h-6 md:w-5 md:h-5 rounded-full bg-white shadow transition-transform",
            isOn ? "translate-x-5 md:translate-x-4" : "translate-x-0",
          )}
        />
      </button>
    </div>
  );
}

export function FanDrawer({
  open, onClose, accountId, fanId, chat,
  pinned = false, alwaysOn = false, overlayHideOnDesktopWhenPinned = false,
  onComposeWith,
}: {
  open: boolean;
  onClose: () => void;
  accountId: string;
  fanId: FanId;
  chat: OFChatItem;
  /** When true, render as an in-flow side column (no backdrop, no
   *  click-out close). Used when the "keep drawer open" flag is on
   *  so the inbox becomes a 3-column layout. */
  pinned?: boolean;
  /** When true (only meaningful in pinned mode), hide the close X
   *  entirely. The popout window uses this so the fan info is always
   *  visible — closing it would defeat the point of the popout. */
  alwaysOn?: boolean;
  /** Overlay-branch only: when true, hide the entire overlay on
   *  `md+` screens. Used in tandem with a pinned sibling — desktop
   *  shows the pinned column, mobile falls back to this overlay. */
  overlayHideOnDesktopWhenPinned?: boolean;
  /** When set, the Unsold-PPVs section's "Send to chat" buttons call
   *  this with a PickedTemplate-shaped payload to populate the parent
   *  Composer's text + attachments + price. Without it, that button
   *  is hidden — the drawer still shows the unsold list as a read-only
   *  preview (useful for the popout window where there's no composer). */
  onComposeWith?: (t: PickedTemplate) => void;
}) {
  const { data: fan, isLoading, update } = useFan(accountId, fanId);
  const accountLabel = useAccountLabel(accountId);
  const ofUserQ = useOFUser(accountId, fanId);
  // Wrapped in a stable single-element array so the underlying useQueries
  // sees the same identity unless accountId actually changes.
  const lastPurchaseAccountIds = useMemo(() => [accountId], [accountId]);
  const activity = useFanActivity(lastPurchaseAccountIds);
  const lastPurchase = activity.lastPurchase.get(`${accountId}:${fanId}`) ?? null;
  const recentSpend = activity.recentSpend.get(`${accountId}:${fanId}`) ?? 0;
  // Ledger page size — DB-side, bumped 20 at a time by Load more.
  const [salesLimit, setSalesLimit] = useState(12);
  const ppvHistoryQ = useFanPpvHistory(accountId, fanId, salesLimit);
  // OF gallery for thumbnails + message text — gated on the drawer
  // being open AND the local ledger showing at least one PPV. Avoids
  // a wasted upstream call on every drawer mount. Cursor-paginated via
  // `last_id`; Load more calls fetchNextPage() so older pages append
  // rather than refetching the entire list.
  const chatMediaQ = useFanChatMedia(accountId, fanId, {
    purchased: true,
    limit: 20,
    enabled: open && (ppvHistoryQ.data?.items.length ?? 0) > 0,
  });
  // Flatten all loaded pages into one list for the row builder.
  const chatMediaFlat = useMemo(
    () => chatMediaQ.data?.pages.flatMap((p) => p.list) ?? [],
    [chatMediaQ.data],
  );

  // Unsold PPVs — same OF endpoint, drop `purchased=1`. We still pin
  // to `from_user=<accountId>` so we get creator-sent items only.
  // Filter for paid + not-yet-opened items; show the latest 2 at the
  // bottom of the drawer. Gated identically to the Sales call.
  const unsoldChatMediaQ = useFanChatMedia(accountId, fanId, {
    purchased: false,
    limit: 20,
    enabled: open,
  });
  const unsoldPpvs = useMemo<FanChatMediaItem[]>(() => {
    const all = unsoldChatMediaQ.data?.pages.flatMap((p) => p.list) ?? [];
    return all
      .filter((m) => (m.price ?? 0) > 0 && m.isOpened === false)
      .sort((a, b) => +new Date(b.createdAt) - +new Date(a.createdAt))
      .slice(0, 2);
  }, [unsoldChatMediaQ.data]);

  // Force-refresh button: blow away the persisted chat-media cache for
  // this fan + refetch both filter modes. Used when the operator knows
  // the fan just unlocked something and doesn't want to wait the 30 min
  // SWR window for the panel to catch up.
  const qc = useQueryClient();
  const refreshChatMedia = () => {
    void qc.invalidateQueries({
      queryKey: ["fan-chat-media", accountId, fanId],
      exact: false,
    });
    // The ledger spine behind the chart + Sales rows. Previously missing
    // here, which made the ↻ button look broken after a fresh sale: the
    // gallery refetched but the chart/Sales list (and "Last purchase")
    // kept rendering the stale ledger read.
    void qc.invalidateQueries({
      queryKey: ["fan-ppv-history", accountId, fanId],
      exact: false,
    });
    void qc.invalidateQueries({ queryKey: ["last-purchases", accountId] });
    // A manual refresh should also re-pull the authoritative fan profile —
    // the live OF read (nickname/notice/spend) and our local mirror — so a
    // nickname/note changed out-of-band (automation, another device) shows up
    // immediately instead of waiting for a window focus or the next edit.
    void qc.invalidateQueries({ queryKey: ["of-user", accountId, fanId] });
    void qc.invalidateQueries({ queryKey: ["fan", accountId, fanId] });
  };

  // The "effective" nickname/note, with OF as the authoritative read (it's what
  // shows on onlyfans.com). ONCE the live OF query has resolved we trust IT — even
  // an empty value — over the chat-list payload, because that payload can be hours
  // stale and would otherwise pin an out-of-date nickname/note after an automation
  // (or another device) changed it. Before the OF query lands we fall back to the
  // chat-list value for an instant first paint, then our local SQLite mirror so the
  // panel never blanks when OF is slow/unreachable. On blur we write through to OF
  // AND our local row so the two stay in sync.
  const ofDisplayName = ofUserQ.isSuccess
    ? (ofUserQ.data?.displayName ?? "")
    : (chat.withUser.displayName ?? "");
  const ofNotice = ofUserQ.isSuccess
    ? (ofUserQ.data?.notice ?? "")
    : (chat.withUser.notice ?? "");
  const effectiveNickname = ofDisplayName || fan?.custom_nickname || "";
  const effectiveNotes = ofNotice || fan?.notes || "";

  // Local form mirrors so blur-save doesn't fight the cache when the user
  // is mid-edit. Reset whenever the effective value (from OF or local) changes.
  const [nickname, setNickname] = useState("");
  const [notes, setNotes] = useState("");
  const [tagsInput, setTagsInput] = useState("");
  // Media lightbox lives at the drawer level (not inside PpvSalesSection)
  // so the drawer's Esc handler can tell when a higher overlay is open and
  // yield to it — Esc should collapse one layer at a time, not nuke both.
  const [lightbox, setLightbox] = useState<LightboxMedia | null>(null);
  // The values we last pushed into the form mirrors. A field is "dirty" (being
  // edited) when its current value no longer equals what we applied — in that
  // case a fresh OF value must NOT overwrite it.
  const appliedRef = useRef({ nick: "", notes: "", tags: "" });
  // Nickname/note are the only fields here that are read back OUT of `of-user`
  // and written over. Opening the drawer no longer refetches that query (see
  // [useOFUser.ts] — the per-open cost on the user lane bought very little), so
  // a value changed elsewhere inside the 5-minute window would prefill stale,
  // get typed over, and save — silently reverting the other change. Focus
  // refetch only covers tab switches, not a same-tab reopen.
  //
  // So we re-read at the moment the operator engages an edit affordance: a
  // deliberate click, orders of magnitude rarer than a drawer open, and the
  // one instant where freshness is load-bearing. It is non-blocking by
  // construction — the field keeps rendering the cached value and the adopt
  // effect below swaps in the fresh one ONLY while nothing has been typed.
  // At most once per staleTime window (per fan): re-reading on every
  // focus/blur cycle would just re-spend the lane we set out to stop
  // spending. Guarded by TIME, not by open — a pinned or popout drawer stays
  // open all day, so a once-per-open boolean would go stale right along with
  // the value it exists to refresh. The window is the query's own
  // OF_USER_STALE_MS because that cache window is precisely what creates
  // the risk this read closes.
  const ofRefetch = ofUserQ.refetch;
  const editRefreshAtRef = useRef(0);
  useEffect(() => { editRefreshAtRef.current = 0; }, [open, accountId, fanId]);
  const refreshBeforeEdit = useCallback(() => {
    if (Date.now() - editRefreshAtRef.current < OF_USER_STALE_MS) return;
    editRefreshAtRef.current = Date.now();
    void ofRefetch();
  }, [ofRefetch]);
  // The OF write-through (custom name / private note) is fire-and-forget and
  // hits a different backend than the local PATCH. When it rejects the local
  // mirror still updates, so without this the panel shows a value OF never
  // accepted and silently reverts the next time of-user is re-read. Surface
  // the failure so the operator knows the OF sync didn't stick.
  const [ofWriteError, setOfWriteError] = useState<string | null>(null);

  useEffect(() => {
    if (!fan) return;
    const tags = (fan.tags ?? []).join(", ");
    // Adopt the freshest effective value ONLY for fields the user hasn't touched.
    // A fresh OF read can land at any time (focus refetch, an invalidation, or
    // the pre-edit refresh above), and a naive reset would clobber an
    // in-progress edit; comparing against the last-applied value keeps the
    // operator's typing while still picking up out-of-band changes.
    const prev = appliedRef.current;
    setNickname((cur) => (cur === prev.nick ? effectiveNickname : cur));
    setNotes((cur) => (cur === prev.notes ? effectiveNotes : cur));
    setTagsInput((cur) => (cur === prev.tags ? tags : cur));
    appliedRef.current = { nick: effectiveNickname, notes: effectiveNotes, tags };
  }, [fan, effectiveNickname, effectiveNotes]);

  // Esc closes the OVERLAY drawer only; in pinned mode it's a real
  // sibling column, so Esc shouldn't kill it. While the media lightbox is
  // open it owns Esc (collapse one layer at a time) — the drawer registers
  // no listener, so a single Esc only closes the lightbox, not the drawer.
  useEffect(() => {
    if (!open || pinned || lightbox) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose, pinned, lightbox]);

  if (!open) return null;

  const displayName =
    ofDisplayName ||
    fan?.custom_nickname ||
    chat.withUser.name ||
    fan?.of_display_name ||
    chat.withUser.username ||
    `fan ${fanId}`;

  const avatar = chat.withUser.avatar || fan?.avatar_url || null;

  // Shared panel body. In overlay mode we wrap it in an absolute-positioned
  // backdrop layer; in pinned mode the parent inbox layout slots it next to
  // the chat column as a normal sibling. The pinned variant is hidden below
  // md (the 3-col layout has no room on phones — the overlay branch shows
  // the same content there).
  const panel = (
    <aside
      className={
        pinned
          ? "hidden md:flex w-[360px] shrink-0 bg-panel border-l border-border flex-col h-full"
          : "w-full max-w-[420px] sm:w-[360px] sm:max-w-[360px] bg-panel border-l border-border shadow-2xl pointer-events-auto flex flex-col"
      }
    >
        <header className="border-b border-border p-4 flex items-start gap-3">
          <div className="w-12 h-12 rounded-full bg-bg-elev-1 overflow-hidden shrink-0 grid place-items-center">
            {avatar ? (
              <img
                src={avatar}
                alt=""
                loading="lazy"
                decoding="async"
                className="w-full h-full object-cover"
              />
            ) : (
              <span className="text-base">
                {displayName.slice(0, 1).toUpperCase()}
              </span>
            )}
          </div>
          <div className="min-w-0 flex-1">
            <div className="text-sm font-semibold truncate">{displayName}</div>
            <div className="text-[11px] text-fg-dim truncate">
              @{chat.withUser.username || fan?.of_username || fanId}
            </div>
            <div className="text-[10px] text-fg-dim mt-0.5">
              {accountLabel} · id {fanId}
            </div>
            <AiSellerChip accountId={accountId} fanId={fanId} />
          </div>
          {!alwaysOn && (
            <button
              type="button"
              onClick={onClose}
              className="shrink-0 md:shrink grid md:block place-items-center w-11 h-11 md:w-auto md:h-auto -mr-2 md:mr-0 -mt-1 md:mt-0 text-2xl md:text-lg leading-none text-fg-dim hover:text-fg"
              aria-label="Close"
            >
              ×
            </button>
          )}
        </header>

        <div className="flex-1 min-h-0 overflow-y-auto p-4 flex flex-col gap-4">
          {isLoading && (
            <div className="text-xs text-fg-dim">Loading fan profile…</div>
          )}

          {fan && (
            <>
              {/* Recency strip — the two signals you scan first when a
               *  chat header is clicked: did they just buy, are they still
               *  in the convo. Promoted out of the bottom stats grid. */}
              <div className="order-first md:order-none grid grid-cols-2 gap-x-4 text-[11px]">
                <Stat label="Last purchase" value={fmtRelTime(lastPurchase)} />
                <Stat label="Last message in" value={fmtRelTime(fan.last_message_received_at)} />
              </div>

              {/* Purchases chart at the top — quick visual of spend
               *  cadence. The detail row list ("Sales") is rendered at
               *  the bottom of the drawer below the stats grid. */}
              <div className="order-2 md:order-none">
                <PpvChartSection
                  items={ppvHistoryQ.data?.items ?? []}
                  maxCents={ppvHistoryQ.data?.summary.max_cents ?? 0}
                  avgCents={ppvHistoryQ.data?.summary.avg_cents ?? 0}
                  loading={ppvHistoryQ.isLoading}
                />
              </div>

              <div className="order-2 md:order-none">
                <ProfileCard fan={fan} />
              </div>

              <div className="order-3 md:order-none flex flex-col gap-4">
              <Field label="Custom nickname" hint="Synced with OnlyFans. Saving updates OF's per-fan custom name and our local mirror.">
                <input
                  type="text"
                  value={nickname}
                  onChange={(e) => setNickname(e.target.value)}
                  onFocus={refreshBeforeEdit}
                  onBlur={() => {
                    const next = nickname.trim();
                    if (next === effectiveNickname.trim()) return;
                    setOfWriteError(null);
                    update.mutate({ custom_nickname: next || null });
                    void writeOFCustomName(accountId, fanId, next).catch((err) => {
                      console.warn("[fan-drawer] OF custom-name write failed", err);
                      setOfWriteError(
                        "Saved locally, but OnlyFans rejected the nickname change — it may revert on the next sync.",
                      );
                    });
                  }}
                  placeholder={chat.withUser.name ?? fan.of_display_name ?? ""}
                  className="w-full bg-bg border border-border rounded-md px-2 py-1.5 text-base md:text-sm focus:outline-none focus:border-accent"
                />
              </Field>

              <Field label="Notes" hint="Synced with OnlyFans' private note. Anything Grok should know later goes here.">
                <textarea
                  value={notes}
                  onChange={(e) => setNotes(e.target.value)}
                  onFocus={refreshBeforeEdit}
                  onBlur={() => {
                    if (notes === effectiveNotes) return;
                    setOfWriteError(null);
                    update.mutate({ notes: notes || null });
                    void writeOFNotice(accountId, fanId, notes).catch((err) => {
                      console.warn("[fan-drawer] OF notice write failed", err);
                      setOfWriteError(
                        "Saved locally, but OnlyFans rejected the note change — it may revert on the next sync.",
                      );
                    });
                  }}
                  rows={6}
                  placeholder="loves bondage, German, paid $500 last week…"
                  className="w-full h-16 md:h-auto bg-bg border border-border rounded-md px-2 py-1.5 text-base md:text-xs focus:outline-none focus:border-accent resize-y"
                />
              </Field>

              <Field label="Tags" hint="Comma-separated. Saves on blur.">
                <input
                  type="text"
                  value={tagsInput}
                  onChange={(e) => setTagsInput(e.target.value)}
                  onBlur={() => {
                    const next = tagsInput
                      .split(",")
                      .map((t) => t.trim())
                      .filter(Boolean);
                    const prev = fan.tags ?? [];
                    if (sameList(next, prev)) return;
                    update.mutate({ tags: next });
                  }}
                  placeholder="whale, EU, kink:feet"
                  className="w-full bg-bg border border-border rounded-md px-2 py-1.5 text-base md:text-xs focus:outline-none focus:border-accent"
                />
                {fan.tags?.length ? (
                  <div className="mt-2 flex flex-wrap gap-1">
                    {fan.tags.map((t) => (
                      <span key={t} className="text-[10px] bg-bg-elev-1 border border-border rounded-full px-2 py-0.5">
                        {t}
                      </span>
                    ))}
                  </div>
                ) : null}
              </Field>

              <Field label="Language" hint={languageHint(fan.language, fan.language_source)}>
                <select
                  value={fan.language ?? ""}
                  onChange={(e) => update.mutate({ language: e.target.value })}
                  className="w-full bg-bg border border-border rounded-md px-2 py-1.5 text-base md:text-sm focus:outline-none focus:border-accent"
                >
                  <option value="">Default (account)</option>
                  {LANGUAGE_OPTIONS.map((l) => (
                    <option key={l.code} value={l.code}>{l.label}</option>
                  ))}
                </select>
              </Field>

              {/* Persona continuity — the claims ledger the chat engines write and
               *  the pre-send consistency check reads. Sits under Notes because it
               *  is the same question ("what does this fan know?") from her side. */}
              <PersonaClaims accountId={accountId} fan={fan}
                             onSave={(patch) => update.mutate(patch)} />
              </div>

              <div className="order-4 md:order-none space-y-1.5">
                <MassExcludeToggle accountId={accountId} fanId={fanId} kind="ppv"
                  title="MASSppvEXCLUDE" hint="Never include in mass PPV sends." />
                <MassExcludeToggle accountId={accountId} fanId={fanId} kind="dm"
                  title="MASSdmEXCLUDE" hint="Never include in mass DMs or online blasts." />
              </div>

              {/* Live numbers pulled from OF's /users/{id}.subscribedOnData,
               *  same path the desktop app uses. Falls back to our DB copy
               *  while the OF call is in flight or errors. */}
              {(() => {
                const sub = ofUserQ.data?.subscribedOnData;
                const liveSpend = typeof sub?.totalSumm === "number"
                  ? Math.round(sub.totalSumm * 100)
                  : null;
                // Prefer OF's authoritative totalSumm, fall back to the
                // transactions-derived windowed sum if it briefly reports 0
                // while our DB copy is also empty.
                const spendCents = Math.max(
                  liveSpend ?? 0,
                  recentSpend,
                  fan.lifetime_spend_cents || 0,
                );
                const status = interpretSubStatus(
                  sub?.status ?? fan.subscription_status,
                  sub?.expiredAt ?? null,
                );
                return (
                  <div className="order-1 md:order-none border-t border-border pt-3 grid grid-cols-2 gap-x-4 gap-y-2 text-[11px]">
                    <Stat label="Lifetime spend" value={fmtUsd(spendCents)} />
                    <Stat label="Sub status" value={status.short} tone={status.tone} />
                    <Stat
                      label="Sub price"
                      value={typeof sub?.price === "number" ? `$${sub.price.toFixed(2)}` : "—"}
                    />
                    <Stat
                      label="Subscribed at"
                      value={fmtRelTime(sub?.subscribeAt ?? fan.subscribed_at)}
                    />
                    <Stat label="Renewed at" value={fmtRelTime(sub?.renewedAt ?? null)} />
                    <Stat
                      label={status.expired ? "Expired" : "Expires"}
                      value={fmtRelTime(sub?.expiredAt ?? null)}
                    />
                    <Stat
                      label="Tips"
                      value={fmtSummDollars(sub?.tipsSumm, ppvHistoryQ.data?.summary.tips_cents)}
                    />
                    <Stat
                      label="Messages"
                      value={fmtSummDollars(sub?.messagesSumm, ppvHistoryQ.data?.summary.messages_cents)}
                    />
                    <Stat
                      label="Posts"
                      value={fmtSummDollars(sub?.postsSumm, ppvHistoryQ.data?.summary.posts_cents)}
                    />
                    <Stat label="Last seen" value={fmtRelTime(ofUserQ.data?.lastSeen as string | null)} />
                  </div>
                );
              })()}

              <div className="order-5 md:order-none">
              <PpvSalesSection
                items={ppvHistoryQ.data?.items ?? []}
                loading={ppvHistoryQ.isLoading}
                chatMedia={chatMediaFlat}
                chatMediaLoading={chatMediaQ.isLoading}
                chatMediaFetching={chatMediaQ.isFetching}
                chatMediaUpdatedAt={chatMediaQ.dataUpdatedAt}
                accountId={accountId}
                salesLimit={salesLimit}
                onLoadMore={() => {
                  setSalesLimit((n) => Math.min(n + 20, 200));
                  // Fetch the next OF gallery page in parallel so the
                  // enrichment keeps up with the ledger.
                  if (chatMediaQ.hasNextPage && !chatMediaQ.isFetchingNextPage) {
                    void chatMediaQ.fetchNextPage();
                  }
                }}
                onRefresh={refreshChatMedia}
                onOpenMedia={setLightbox}
                serverFetching={
                  ppvHistoryQ.isFetching || chatMediaQ.isFetchingNextPage
                }
              />
              </div>

              {/* md:contents (not md:order-none): UnsoldPpvsSection renders
               *  null when there is nothing unsold, and an empty wrapper
               *  would still consume a gap-4 slot on desktop. */}
              <div className="order-6 md:contents">
                <UnsoldPpvsSection
                  items={unsoldPpvs}
                  loading={unsoldChatMediaQ.isLoading}
                  accountId={accountId}
                  onComposeWith={onComposeWith}
                />
              </div>

              {update.error && (
                <div className="text-err text-xs">
                  {(update.error as Error).message || "save failed"}
                </div>
              )}
              {ofWriteError && (
                <div className="text-err text-xs">{ofWriteError}</div>
              )}
            </>
          )}
        </div>
        {lightbox && (
          <MediaLightbox media={lightbox} onClose={() => setLightbox(null)} />
        )}
      </aside>
  );

  if (pinned) return panel;
  return (
    <div
      className={cn(
        "absolute inset-0 z-30 flex pointer-events-none",
        overlayHideOnDesktopWhenPinned && "md:hidden",
      )}
    >
      <div className="flex-1 min-w-14 sm:min-w-0 bg-black/40 pointer-events-auto" onClick={onClose} />
      {panel}
    </div>
  );
}

function Field({
  label, hint, children,
}: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <div className="space-y-1">
      <div className="flex items-baseline justify-between">
        <label className="text-[11px] font-medium text-fg-dim uppercase tracking-wide">{label}</label>
      </div>
      {children}
      {hint && <div className="hidden md:block text-[10px] text-fg-dim">{hint}</div>}
    </div>
  );
}

function Stat({
  label, value, tone,
}: {
  label: string;
  value: string;
  tone?: "ok" | "warn" | "err" | "fg-dim";
}) {
  const toneClass =
    tone === "ok" ? "text-ok"
    : tone === "warn" ? "text-warn"
    : tone === "err" ? "text-err"
    : tone === "fg-dim" ? "text-fg-dim"
    : "text-fg";
  return (
    <div>
      <div className="text-fg-dim">{label}</div>
      <div className={`${toneClass} font-medium`}>{value}</div>
    </div>
  );
}

function fmtUsd(cents: number | null | undefined): string {
  if (!cents) return "$0";
  return `$${(cents / 100).toFixed(2)}`;
}

/** Whole-dollar formatter for the stats grid summaries. OF returns
 *  these as dollars (`tipsSumm: 87`); our DB returns cents. Prefer the
 *  OF number when it's a real value; fall back to DB cents otherwise.
 *  We treat `0` as "missing" on the OF side because OF reports 0 for
 *  long-tenure / hidden accounts even when activity exists — better to
 *  show our DB total than a misleading $0. */
function fmtSummDollars(ofDollars: number | null | undefined, dbCents: number | null | undefined): string {
  if (typeof ofDollars === "number" && ofDollars > 0) return `$${ofDollars.toFixed(0)}`;
  if (typeof dbCents === "number" && dbCents > 0) return `$${(dbCents / 100).toFixed(0)}`;
  return "—";
}

/** How many sale rows to render before "show more". */
const INITIAL_SALE_ROWS = 3;

/** Chart-only section — rendered at the top of the drawer. Carries the
 *  Max/Avg summary inline with the heading so the top strip remains a
 *  single-glance read. The sales detail list lives in PpvSalesSection
 *  at the bottom of the drawer. */
function PpvChartSection({
  items, maxCents, avgCents, loading,
}: {
  items: PpvHistoryItem[];
  maxCents: number;
  avgCents: number;
  loading: boolean;
}) {
  return (
    <div className="space-y-2">
      <div className="flex items-baseline justify-between gap-2">
        <div className="text-[11px] font-medium text-fg-dim uppercase tracking-wide">
          PPV purchases by fan
        </div>
        <div className="text-[10px] text-fg-dim flex gap-3">
          <span>Max <span className="text-warn font-medium">{fmtUsd(maxCents)}</span></span>
          <span>Avg <span className="text-warn font-medium">{fmtUsd(avgCents)}</span></span>
        </div>
      </div>
      <div className="hidden md:block">
        {loading && items.length === 0 ? (
          <div className="text-[10px] text-fg-dim h-24 flex items-center">Loading purchase history…</div>
        ) : items.length === 0 ? (
          <div className="text-[10px] text-fg-dim h-24 flex items-center">No PPV purchases yet.</div>
        ) : (
          <PpvHistoryChart items={items} maxCents={maxCents} />
        )}
      </div>
    </div>
  );
}

/** Sales detail list — rendered at the bottom of the drawer, below the
 *  stats grid. Renders newest-first; first `INITIAL_SALE_ROWS` shown,
 *  remainder revealed via a "Show N more" button at the BOTTOM (so
 *  scrolling down naturally exposes more, the way infinite-scroll
 *  feeds work). */
function PpvSalesSection({
  items, loading,
  chatMedia, chatMediaLoading, chatMediaFetching, chatMediaUpdatedAt,
  accountId,
  salesLimit, onLoadMore, onRefresh, onOpenMedia, serverFetching,
}: {
  items: PpvHistoryItem[];
  loading: boolean;
  chatMedia: FanChatMediaItem[];
  chatMediaLoading: boolean;
  /** True whenever React Query is fetching ANY page (initial OR
   *  background SWR refetch). Spins the refresh button while active. */
  chatMediaFetching: boolean;
  /** ms epoch the cached chat-media was last refreshed. 0 if never. */
  chatMediaUpdatedAt: number;
  accountId: string;
  /** Current page size requested from both ppv-history + chat-media. */
  salesLimit: number;
  /** Bumps the page size in the parent. Hidden once we've hit the
   *  server cap or the ledger has clearly returned fewer rows than
   *  requested (= no more to load). */
  onLoadMore: () => void;
  /** Invalidates the chat-media cache and refetches both purchased +
   *  unsold filter modes. Wired to the ↻ button next to "Sales". */
  onRefresh: () => void;
  /** Opens a media tile in the drawer-level lightbox. Lifted to the
   *  drawer so its Esc handler can yield to the open lightbox. */
  onOpenMedia: (m: LightboxMedia) => void;
  /** True while either upstream is mid-refetch — used to disable the
   *  Load-more button so a fast double-click doesn't queue two bumps. */
  serverFetching: boolean;
}) {
  // OF gallery is the spine (thumbnails + text); ledger items get
  // fuzzy-matched on top for accounting/employee. Falls back to a
  // ledger-only render when OF hasn't responded yet so the section
  // never blanks.
  const detailRows = buildSaleRows(chatMedia, items);

  const [showAll, setShowAll] = useState(false);
  const [expandedKeys, setExpandedKeys] = useState<Set<string>>(() => new Set());

  // Delay the "loading media…" text by 300ms so cached data — which resolves
  // almost instantly — renders its timestamp directly instead of flashing the
  // loading state for a frame or two.
  const [showLoading, setShowLoading] = useState(false);
  useEffect(() => {
    if (!(chatMediaLoading && chatMedia.length === 0)) {
      setShowLoading(false);
      return;
    }
    const t = window.setTimeout(() => setShowLoading(true), 300);
    return () => window.clearTimeout(t);
  }, [chatMediaLoading, chatMedia.length]);

  const toggleExpanded = (key: string) => {
    setExpandedKeys((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key); else next.add(key);
      return next;
    });
  };

  const visibleRows = showAll ? detailRows : detailRows.slice(0, INITIAL_SALE_ROWS);
  const hiddenCount = detailRows.length - visibleRows.length;

  return (
    <div className="space-y-2 border-t border-border pt-3">
      <div className="flex items-baseline justify-between gap-2">
        <div className="text-[11px] font-medium text-fg-dim uppercase tracking-wide">
          Sales
        </div>
        <div className="flex items-center gap-2">
          {showLoading && (
            <span className="text-[9px] text-fg-dim">loading media…</span>
          )}
          {!chatMediaLoading && chatMediaUpdatedAt > 0 && (
            <span className="text-[9px] text-fg-dim" title={new Date(chatMediaUpdatedAt).toLocaleString()}>
              {fmtRelTime(new Date(chatMediaUpdatedAt).toISOString())}
            </span>
          )}
          <button
            type="button"
            onClick={onRefresh}
            disabled={chatMediaFetching}
            title="Refresh OnlyFans gallery now (otherwise auto-refreshes after 30 min)"
            aria-label="Refresh gallery"
            className={cn(
              "w-9 h-9 md:w-auto md:h-auto grid md:block place-items-center text-fg-dim hover:text-fg text-base md:text-[12px] leading-none disabled:opacity-50",
              chatMediaFetching && "animate-spin",
            )}
          >
            ↻
          </button>
        </div>
      </div>
      {loading && items.length === 0 ? (
        <div className="text-[10px] text-fg-dim">Loading purchase history…</div>
      ) : items.length === 0 ? (
        <div className="text-[10px] text-fg-dim">No PPV purchases yet.</div>
      ) : (
        <div className="space-y-2">
          {visibleRows.map((row) => (
            <PpvSaleRow
              key={row.key}
              row={row}
              accountId={accountId}
              expanded={expandedKeys.has(row.key)}
              onToggle={() => toggleExpanded(row.key)}
              onOpenMedia={onOpenMedia}
            />
          ))}
          {/* Show-more lives at the BOTTOM — newest-first feed pattern,
           *  scroll down to load older. */}
          {hiddenCount > 0 && !showAll && (
            <button
              type="button"
              onClick={() => setShowAll(true)}
              className="w-full text-xs md:text-[10px]/[1.5] text-fg-dim hover:text-fg border border-dashed border-border rounded-md py-3 md:py-1.5"
            >
              Show {hiddenCount} more
            </button>
          )}
          {showAll && detailRows.length > INITIAL_SALE_ROWS && (
            <button
              type="button"
              onClick={() => setShowAll(false)}
              className="w-full text-xs md:text-[10px]/[1.5] text-fg-dim hover:text-fg border border-dashed border-border rounded-md py-3 md:py-1.5"
            >
              Show less
            </button>
          )}
          {/* Server-side pagination. Only meaningful once everything
           *  already fetched is visible (i.e. user has expanded the
           *  client-side hidden rows) — otherwise "Load more" would
           *  confuse with "Show more". Hidden when the ledger came back
           *  with fewer rows than requested (no more to fetch) OR we're
           *  at the server's hard cap. */}
          {showAll
            && items.length >= salesLimit
            && salesLimit < 200 && (
            <button
              type="button"
              onClick={onLoadMore}
              disabled={serverFetching}
              className="w-full text-sm md:text-[11px]/[1.5] text-accent hover:text-accent-hover border border-dashed border-accent/50 rounded-md py-3 md:py-1.5 disabled:opacity-50 disabled:cursor-progress"
            >
              {serverFetching ? "Loading…" : "Load more from server"}
            </button>
          )}
        </div>
      )}
    </div>
  );
}

/** Latest 2 unsold PPVs the creator has sent this fan. Each row has a
 *  "Send to chat" button that populates the parent Composer with the
 *  exact same payload (text + media + price) so the operator can re-pitch
 *  the unlock in one click. Hidden entirely when there are no unsold
 *  PPVs OR when the parent didn't wire `onComposeWith` (= read-only). */
function UnsoldPpvsSection({
  items, loading, accountId, onComposeWith,
}: {
  items: FanChatMediaItem[];
  loading: boolean;
  accountId: string;
  onComposeWith?: (t: PickedTemplate) => void;
}) {
  if (loading && items.length === 0) return null;
  if (items.length === 0) return null;
  return (
    <div className="space-y-2 border-t border-border pt-3">
      <div className="text-[11px] font-medium text-fg-dim uppercase tracking-wide">
        Unsold PPVs sent
      </div>
      {items.map((m) => (
        <UnsoldPpvRow
          key={m.id}
          item={m}
          accountId={accountId}
          onComposeWith={onComposeWith}
        />
      ))}
    </div>
  );
}

function UnsoldPpvRow({
  item, accountId, onComposeWith,
}: {
  item: FanChatMediaItem;
  accountId: string;
  onComposeWith?: (t: PickedTemplate) => void;
}) {
  const text = stripOFHtml(item.text);
  const priceCents = Math.round((item.price ?? 0) * 100);
  const thumbs = pickThumbs(item.media);
  const handleSend = () => {
    if (!onComposeWith) return;
    // Synthesize a PickedTemplate so Composer's existing onPickTemplate
    // handler does the full replace (text + attached vault media + price
    // + lock + previewCount). VaultMedia ids ARE the same ids OF returns
    // on chat-media `media[i].id`, so attaching by id Just Works on send.
    // VaultFile.url is `string` (non-null); chat-media files have
    // `string | null | undefined`. Coerce by dropping entries whose url
    // is missing — the chip will render a placeholder until OF refreshes
    // the signed url, but send-by-id won't care either way.
    const toVaultFile = (
      f: { url?: string | null } | null | undefined,
    ) => (f && f.url ? { url: f.url } : undefined);
    const media: VaultMedia[] = item.media.map((mm) => ({
      id: mm.id,
      type: (mm.type as VaultMedia["type"]) || "photo",
      duration: mm.duration,
      files: mm.files
        ? {
            thumb: toVaultFile(mm.files.thumb),
            preview: toVaultFile(mm.files.preview),
            squarePreview: toVaultFile(mm.files.squarePreview),
            full: toVaultFile(mm.files.full),
          }
        : null,
    }));
    onComposeWith({
      source: "of",
      text,
      displayText: item.text,
      mediaCount: item.mediaCount ?? media.length,
      price: item.price ?? 0,
      // OF chat-media doesn't expose lockedText separately; we leave it
      // off so the resend defaults to unlocked-text, matching what the
      // operator usually wants for a follow-up pitch.
      lockedText: false,
      isWelcome: false,
      media,
      previews: [],
      taggedUsers: [],
    });
  };
  return (
    <div className="rounded-md bg-bg-elev-1 border border-border overflow-hidden">
      <div className="flex items-center gap-2 px-2 py-1.5">
        <span className="inline-flex items-center gap-1 bg-warn text-white text-[11px] font-semibold rounded-full px-2 py-0.5">
          <span className="opacity-90">$</span>{(priceCents / 100).toFixed(2)}
        </span>
        <span className="text-[11px] text-fg-dim flex-1 min-w-0 truncate">
          {fmtFullDateTime(item.createdAt)}
        </span>
        {onComposeWith && (
          <button
            type="button"
            onClick={handleSend}
            className="text-[10px] bg-accent hover:bg-accent-hover text-white rounded px-2 py-0.5"
            title="Load this PPV into the composer"
          >
            Send to chat
          </button>
        )}
      </div>
      {thumbs.length > 0 && (
        <div className="flex gap-1.5 overflow-x-auto px-2 pb-2 -mt-0.5">
          {thumbs.map((t, i) => (
            <div
              key={i}
              className="relative shrink-0 w-16 h-16 rounded-md bg-bg-elev-2 border border-border overflow-hidden"
            >
              {t.rawUrl ? (
                <img
                  src={proxyImage(t.rawUrl, accountId)}
                  alt=""
                  loading="lazy"
                  decoding="async"
                  className="w-full h-full object-cover"
                />
              ) : (
                <div className="w-full h-full grid place-items-center text-[9px] text-fg-dim">—</div>
              )}
              {t.isVideo && (
                <span className="absolute inset-0 grid place-items-center bg-black/20 text-white text-base pointer-events-none">
                  ▶
                </span>
              )}
            </div>
          ))}
        </div>
      )}
      {text && (
        <div className="px-2 pb-2 text-[11px] text-fg-dim line-clamp-2 whitespace-pre-wrap break-words">
          {text}
        </div>
      )}
    </div>
  );
}

interface PpvSaleThumb {
  /** Raw signed OF CDN url — must be wrapped in proxyImage before rendering. */
  rawUrl: string;
  /** Best full-size url for the lightbox (full → preview → thumb). */
  rawFullUrl: string;
  /** Playable signed video URL for videos (used as both the hover-scrub
   *  source and the lightbox <video src>). Empty for photos. */
  rawVideoUrl: string;
  /** Video duration in seconds — lets the relay's storyboard extractor
   *  skip its own ffprobe step. */
  durationSec: number;
  isVideo: boolean;
}

interface PpvSaleRowData {
  key: string;
  /** OF message id when available (OF gallery row), null for ledger-only fallback. */
  ofMessageId: number | null;
  text: string;
  priceCents: number;
  purchasedAt: string | null;
  thumbs: PpvSaleThumb[];
  // DB enrichment — null if no fuzzy match.
  netCents: number | null;
  feeCents: number | null;
  vatCents: number | null;
  employeeName: string | null;
  employeeColor: string | null;
}

interface LightboxMedia {
  proxiedUrl: string;
  isVideo: boolean;
}

function PpvSaleRow({
  row, accountId, expanded, onToggle, onOpenMedia,
}: {
  row: PpvSaleRowData;
  accountId: string;
  expanded: boolean;
  onToggle: () => void;
  onOpenMedia: (m: LightboxMedia) => void;
}) {
  const accounting = formatAccounting(row);
  return (
    <div className="rounded-md bg-bg-elev-1 border border-border overflow-hidden">
      {/* Header row — green price pill + full date + expand chevron.
       *  Clicking anywhere on this strip toggles the body text. */}
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={expanded}
        className="w-full flex items-center gap-2 px-2 py-1.5 text-left hover:bg-bg-elev-2/40"
      >
        <span className="inline-flex items-center gap-1 bg-ok text-white text-[11px] font-semibold rounded-full px-2 py-0.5">
          <span className="opacity-90">$</span>{(row.priceCents / 100).toFixed(2)}
        </span>
        <span className="text-[11px] text-fg-dim flex-1 min-w-0 truncate">
          {fmtFullDateTime(row.purchasedAt)}
        </span>
        <span className={cn("text-fg-dim text-[10px] transition-transform", expanded && "rotate-180")}>
          ▾
        </span>
      </button>

      {/* Media strip — horizontal scroll, vault-style square tiles.
       *  Click a tile to open it in the lightbox. Videos hover-scrub. */}
      {row.thumbs.length > 0 && (
        <div className="flex gap-1.5 overflow-x-auto px-2 pb-2 -mt-0.5 scrollbar-thin">
          {row.thumbs.map((t, i) => {
            const openInLightbox = () => {
              if (!t.rawFullUrl && !t.rawUrl && !t.rawVideoUrl) return;
              onOpenMedia({
                // Videos: prefer the playable mp4 so the lightbox's
                // <video> can stream it. Photos fall through to the
                // best still.
                proxiedUrl: proxyImage(
                  t.isVideo ? (t.rawVideoUrl || t.rawFullUrl || t.rawUrl)
                            : (t.rawFullUrl || t.rawUrl),
                  accountId,
                ),
                isVideo: t.isVideo,
              });
            };
            return t.isVideo && t.rawVideoUrl ? (
              <VideoScrubThumb
                key={i}
                thumb={t}
                accountId={accountId}
                onClick={openInLightbox}
              />
            ) : (
              <button
                key={i}
                type="button"
                onClick={(e) => { e.stopPropagation(); openInLightbox(); }}
                className="relative shrink-0 w-20 h-20 rounded-md bg-bg-elev-2 border border-border overflow-hidden group"
                title="Click to preview"
              >
                {t.rawUrl ? (
                  <img
                    src={proxyImage(t.rawUrl, accountId)}
                    alt=""
                    loading="lazy"
                    decoding="async"
                    className="w-full h-full object-cover transition-transform group-hover:scale-105"
                  />
                ) : (
                  <div className="w-full h-full grid place-items-center text-[9px] text-fg-dim">—</div>
                )}
                {t.isVideo && (
                  <span className="absolute inset-0 grid place-items-center bg-black/20 text-white text-lg pointer-events-none">
                    ▶
                  </span>
                )}
              </button>
            );
          })}
        </div>
      )}

      {/* Expanded body — full text + accounting/employee. The screenshot's
       *  "Read more" is implicit here: header click opens / closes. */}
      {expanded && (
        <div className="px-2 pb-2 space-y-1 text-[11px]">
          {row.text ? (
            <div className="text-fg whitespace-pre-wrap break-words">{row.text}</div>
          ) : row.thumbs.length > 0 ? (
            // OF matched but no caption — just media. Don't say
            // "media only" as a body since the thumbs above already
            // make that obvious.
            null
          ) : (
            // No OF match at all. The accounting line still shows
            // below; the user knows what was paid even without the
            // captioned content cached. Common causes: the OF gallery
            // page that contains this sale hasn't been loaded yet (try
            // Load more), or the message was deleted on OF.
            <div className="text-fg-dim italic">
              No matching OnlyFans gallery item — try Load more, or the
              original message may have been deleted.
            </div>
          )}
          {(accounting || row.employeeName) && (
            <div className="text-[10px] text-fg-dim flex flex-wrap gap-x-2 pt-0.5 border-t border-border/60">
              {accounting && <span>{accounting}</span>}
              {row.employeeName && (
                <span style={row.employeeColor ? { color: row.employeeColor } : undefined}>
                  · {row.employeeName}
                </span>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// Scrub-frame slideshow constants — match VaultPicker so the relay's
// /img/scrub disk cache is reused across surfaces (the relay keys cache
// entries by mp4 hash + frame index, not by caller).
const SCRUB_FRAMES = 12;
const SCRUB_INTERVAL_MS = 600;
const HOVER_DELAY_MS = 400;

/** Video thumb with vault-style hover scrub. Hover-intent debounce
 *  (HOVER_DELAY_MS) kills brush-past thrash; once committed we render
 *  all 12 storyboard frames as overlayed <img> with opacity toggled,
 *  cycling every SCRUB_INTERVAL_MS. Frame fetches go through the
 *  relay's /img/scrub endpoint which builds the storyboard once and
 *  serves cached JPGs on subsequent hovers — so a re-hover is free.
 *  On mouseleave we fire-and-forget a /img/scrub/cancel POST so the
 *  relay can abort the build if it's still mid-download. */
function VideoScrubThumb({
  thumb, accountId, onClick,
}: {
  thumb: PpvSaleThumb;
  accountId: string;
  onClick: () => void;
}) {
  const [hovered, setHovered] = useState(false);
  const [scrubFrameIdx, setScrubFrameIdx] = useState(0);
  const [firstFrameReady, setFirstFrameReady] = useState(false);
  // Countdown shown over the shimmer while the relay extracts the
  // storyboard for a cold video — copied formula from VaultPicker
  // (calibrated against real cold-build timings). Estimate ticks
  // down 5×/sec; if the frame arrives before zero, the whole overlay
  // unmounts via `firstFrameReady`; if zero hits first we show "…".
  const [countdownSec, setCountdownSec] = useState<number>(0);
  // Per-hover session id — forwarded to BOTH the /img/scrub frame
  // requests AND the cancel POST so the relay can scope cancellation
  // to this exact hover session. A delayed cancel from a previous
  // hover of the same video can't reach into a freshly-started build.
  const [sessionId, setSessionId] = useState<string>("");
  const hoverTimerRef = useRef<number | null>(null);
  // Touch devices synthesise mouseenter on tap, so every tap on this thumb
  // would commit a 12-frame relay ffmpeg storyboard build (and the cancel
  // POST rides on mouseleave, which may never fire) while the click opens
  // the lightbox on top of it. Real pointers keep the hover-scrub exactly
  // as it is today.
  const [canHover] = useState(
    () =>
      typeof window !== "undefined" &&
      window.matchMedia("(hover: hover) and (pointer: fine)").matches,
  );

  const start = () => {
    if (hoverTimerRef.current != null) window.clearTimeout(hoverTimerRef.current);
    hoverTimerRef.current = window.setTimeout(() => {
      hoverTimerRef.current = null;
      const sid =
        typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
          ? crypto.randomUUID()
          : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
      setSessionId(sid);
      setScrubFrameIdx(0);
      setFirstFrameReady(false);
      setHovered(true);
    }, HOVER_DELAY_MS);
  };
  const stop = () => {
    if (hoverTimerRef.current != null) {
      window.clearTimeout(hoverTimerRef.current);
      hoverTimerRef.current = null;
    }
    setHovered(false);
  };

  // Initialize the countdown estimate the moment hover commits. Same
  // piecewise formula VaultPicker uses (≤60s clips: ~5-7s; >60s clips:
  // 6.5s + 7s per minute past the first). Tick 5×/sec so the number
  // feels live.
  useEffect(() => {
    if (!hovered) {
      setCountdownSec(0);
      return;
    }
    const dur = Math.max(1, thumb.durationSec || 0);
    const estimate = dur <= 60
      ? Math.max(4, Math.round(4.5 + 0.5 * Math.ceil(dur / 15)))
      : Math.max(4, Math.round(6.5 + (7 * (dur - 60)) / 60));
    setCountdownSec(estimate);
    const startedAt = Date.now();
    const tickId = window.setInterval(() => {
      const elapsed = (Date.now() - startedAt) / 1000;
      setCountdownSec(Math.max(0, estimate - elapsed));
    }, 200);
    return () => window.clearInterval(tickId);
  }, [hovered, thumb.durationSec]);

  // Slideshow cursor — only ticks while hovered AND the first frame
  // has actually painted (so we don't show invisible frames over the
  // thumb during the cold-build wait).
  useEffect(() => {
    if (!hovered || !firstFrameReady) return;
    const id = window.setInterval(() => {
      setScrubFrameIdx((n) => (n + 1) % SCRUB_FRAMES);
    }, SCRUB_INTERVAL_MS);
    return () => window.clearInterval(id);
  }, [hovered, firstFrameReady]);

  // Cancel the relay-side build when the user moves on before the
  // first frame is ready. `keepalive: true` lets the request survive
  // an immediate unmount (e.g. clicked away from the drawer).
  useEffect(() => {
    if (hovered || !sessionId || !thumb.rawVideoUrl) return;
    const tok = typeof window !== "undefined"
      ? new URLSearchParams(window.location.search).get("t")
        ?? window.localStorage.getItem("chatterly:share_token")
      : null;
    const qs = new URLSearchParams();
    qs.set("u", thumb.rawVideoUrl);
    qs.set("sid", sessionId);
    if (tok) qs.set("t", tok);
    fetch(`/img/scrub/cancel?${qs.toString()}`, {
      method: "POST",
      keepalive: true,
    }).catch(() => { /* fire-and-forget */ });
  }, [hovered, sessionId, thumb.rawVideoUrl]);

  return (
    <button
      type="button"
      onClick={(e) => { e.stopPropagation(); onClick(); }}
      onMouseEnter={canHover ? start : undefined}
      onMouseLeave={canHover ? stop : undefined}
      className="relative shrink-0 w-20 h-20 rounded-md bg-bg-elev-2 border border-border overflow-hidden group"
      title={canHover ? "Hover to scrub, click to play" : "Tap to play"}
    >
      {/* Static thumb — always rendered as the loading state /
       *  fallback when frames fail. Frames fade in on top. */}
      {thumb.rawUrl ? (
        <img
          src={proxyImage(thumb.rawUrl, accountId)}
          alt=""
          loading="lazy"
          decoding="async"
          className="absolute inset-0 w-full h-full object-cover"
        />
      ) : (
        <div className="absolute inset-0 grid place-items-center text-[9px] text-fg-dim">—</div>
      )}
      {hovered && sessionId &&
        Array.from({ length: SCRUB_FRAMES }).map((_, idx) => (
          <img
            // Per-frame key bound to the session id so re-hovers force
            // fresh elements — prevents a stale paint from the previous
            // hover bleeding into the new session.
            key={`${sessionId}-${idx}`}
            src={proxyScrubFrame(
              thumb.rawVideoUrl,
              accountId,
              idx,
              thumb.durationSec,
              sessionId,
            )}
            alt=""
            aria-hidden={idx !== scrubFrameIdx}
            decoding="async"
            // Clear the shimmer on the first frame that paints — keying off
            // ANY frame (not just frame 0) means a single failed frame-0
            // fetch can't pin the shimmer forever. The setter is idempotent.
            onLoad={() => setFirstFrameReady(true)}
            // Belt-and-suspenders: if frame 0 itself errors, drop the shimmer
            // so we fall back to the static thumb instead of hanging on it.
            onError={idx === 0 ? () => setFirstFrameReady(true) : undefined}
            style={{
              opacity: idx === scrubFrameIdx && firstFrameReady ? 1 : 0,
            }}
            className="absolute inset-0 w-full h-full object-cover transition-opacity duration-200"
          />
        ))}
      {/* Cold-build shimmer + countdown — covers the thumb while the
       *  relay is extracting the storyboard. Unmounts as soon as the
       *  first frame paints (firstFrameReady → slideshow takes over).
       *  Cached videos go straight to firstFrameReady on the first
       *  frame's onLoad so the shimmer is invisible there. */}
      {hovered && !firstFrameReady && (
        <>
          <div aria-hidden className="scrub-shimmer pointer-events-none" />
          <div
            aria-hidden
            className="absolute inset-0 grid place-items-center pointer-events-none"
          >
            <span className="text-white/85 text-base font-semibold drop-shadow-[0_1px_3px_rgba(0,0,0,0.7)]">
              {countdownSec > 0 ? `${Math.ceil(countdownSec)}s` : "…"}
            </span>
          </div>
        </>
      )}
      {!hovered && (
        <span className="absolute inset-0 grid place-items-center bg-black/20 text-white text-lg pointer-events-none">
          ▶
        </span>
      )}
    </button>
  );
}

function MediaLightbox({ media, onClose }: { media: LightboxMedia; onClose: () => void }) {
  // Close on Esc — mirrors the drawer's own Esc handling so the keyboard
  // story is consistent (Esc collapses one layer at a time).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div
      className="fixed inset-0 z-50 bg-black/80 flex items-center justify-center p-4"
      onClick={onClose}
    >
      {media.isVideo ? (
        <video
          src={media.proxiedUrl}
          controls
          autoPlay
          playsInline
          className="max-w-full max-h-full rounded-md"
          onClick={(e) => e.stopPropagation()}
        />
      ) : (
        <img
          src={media.proxiedUrl}
          alt=""
          className="max-w-full max-h-full rounded-md"
          onClick={(e) => e.stopPropagation()}
        />
      )}
      <button
        type="button"
        onClick={onClose}
        className="absolute right-3 top-[calc(0.75rem_+_env(safe-area-inset-top))] md:top-3 text-white text-2xl leading-none w-11 h-11 md:w-9 md:h-9 grid place-items-center rounded-full bg-black/40 hover:bg-black/60"
        aria-label="Close preview"
      >
        ×
      </button>
    </div>
  );
}

function fmtFullDateTime(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  // "May 24, 2026 at 7:04 PM" — matches the OF screenshot phrasing.
  const datePart = d.toLocaleDateString(undefined, {
    month: "short", day: "numeric", year: "numeric",
  });
  const timePart = d.toLocaleTimeString(undefined, {
    hour: "numeric", minute: "2-digit",
  });
  return `${datePart} at ${timePart}`;
}

function formatAccounting(row: PpvSaleRowData): string | null {
  const parts: string[] = [];
  if (row.netCents != null) parts.push(`net ${fmtUsd(row.netCents)}`);
  if (row.feeCents != null) parts.push(`fee ${fmtUsd(row.feeCents)}`);
  if (row.vatCents != null && row.vatCents !== 0) parts.push(`VAT ${fmtUsd(row.vatCents)}`);
  return parts.length ? parts.join(" · ") : null;
}

function pickThumbs(media: FanChatMediaItem["media"]): PpvSaleThumb[] {
  if (!media) return [];
  return media.map((m) => {
    const f = m.files ?? {};
    const thumbUrl =
      f.squarePreview?.url ?? f.thumb?.url ?? f.preview?.url ?? f.full?.url ?? "";
    // Lightbox prefers the full asset; falls back to preview then thumb
    // so a missing `full` (common on locked / no-longer-purchasable media)
    // still gives us something to render in the overlay.
    const fullUrl =
      f.full?.url ?? f.preview?.url ?? f.squarePreview?.url ?? f.thumb?.url ?? "";
    const isVideo = m.type === "video";
    // For videos OF puts the playable mp4 under files.full; preview is a
    // shorter teaser clip on some items, full on others. The relay's
    // /img/scrub builder needs a real mp4, so prefer full → preview.
    const videoUrl = isVideo ? (f.full?.url ?? f.preview?.url ?? "") : "";
    return {
      rawUrl: thumbUrl ?? "",
      rawFullUrl: fullUrl ?? "",
      rawVideoUrl: videoUrl ?? "",
      durationSec: typeof m.duration === "number" ? m.duration : 0,
      isVideo,
    };
  });
}

/** True when OF's list price (in cents) plausibly corresponds to a
 *  ledger row's gross amount. The relationship isn't always 1:1:
 *    • creator sets list price X (e.g. $18.00)
 *    • OF charges fan X + VAT (varies by region, ~12% in EU)
 *    • OF returns `price: 18` on the gallery row regardless
 *    • our ledger row carries amount_cents = gross paid by fan
 *      (sometimes $18, sometimes $18.03 due to FX rounding, $20.16
 *      with VAT, etc.)
 *
 *  We accept a match if the OF price equals any of the following
 *  derivations of the ledger amount, ±2¢ rounding tolerance:
 *    • amount_cents               (no-VAT region; common case)
 *    • amount_cents − vat_cents   (when ledger amount is VAT-inclusive)
 *    • amount_cents + vat_cents   (when ledger amount is ex-VAT)
 *    • net_cents + fee_cents      (some Phase-F rows store these split)
 *  Plus a ±10% loose fallback to catch OF's occasional ad-hoc discount
 *  rounding. The strict candidates win first; loose only fires when no
 *  strict match exists in the whole gallery. */
function priceMatchScore(ofCents: number, it: PpvHistoryItem): "strict" | "loose" | null {
  const lc = it.price_cents;
  const candidates: number[] = [lc];
  if (it.vat_cents) {
    candidates.push(lc - it.vat_cents);
    candidates.push(lc + it.vat_cents);
  }
  if (it.net_cents != null && it.fee_cents != null) {
    candidates.push(it.net_cents + it.fee_cents);
  }
  for (const c of candidates) {
    if (Math.abs(ofCents - c) <= 2) return "strict";
  }
  if (lc > 0 && Math.abs(ofCents - lc) / lc < 0.10) return "loose";
  return null;
}

/** Unified row builder — ledger is the spine (source of truth for what
 *  was paid), OF gallery enriches with thumbnails + text.
 *
 *  Matching pass order:
 *    1. ledger.message_id == OF.id  (definitive, when ledger has the id)
 *    2. price strict-match (any of the candidate derivations ±2¢),
 *       nearest |createdAt − purchased_at| wins
 *    3. price loose-match (±10%), nearest time wins
 *
 *  Each OF item is consumed at most once so two same-priced ledger rows
 *  on the same day can't both claim a single gallery entry. */
function buildSaleRows(
  media: FanChatMediaItem[],
  ledger: PpvHistoryItem[],
): PpvSaleRowData[] {
  const sortedLedger = [...ledger].sort(
    (a, b) =>
      +new Date(b.purchased_at ?? b.sent_at ?? 0) -
      +new Date(a.purchased_at ?? a.sent_at ?? 0),
  );
  const mediaById = new Map(media.map((m) => [m.id, m] as const));
  const usedMediaIds = new Set<number>();

  function chooseMedia(it: PpvHistoryItem): FanChatMediaItem | null {
    // Pass 1: hard message-id match.
    if (it.message_id != null) {
      const direct = mediaById.get(it.message_id);
      if (direct && !usedMediaIds.has(direct.id)) return direct;
    }
    // Pass 2+3: collect strict and loose price candidates, then pick by
    // time-proximity within the tighter bucket.
    const ts = +new Date(it.purchased_at ?? it.sent_at ?? 0);
    let bestStrict: FanChatMediaItem | null = null;
    let bestStrictDelta = Number.POSITIVE_INFINITY;
    let bestLoose: FanChatMediaItem | null = null;
    let bestLooseDelta = Number.POSITIVE_INFINITY;
    for (const m of media) {
      if (usedMediaIds.has(m.id)) continue;
      const ofCents = Math.round((m.price ?? 0) * 100);
      const score = priceMatchScore(ofCents, it);
      if (!score) continue;
      const mTs = +new Date(m.createdAt);
      const delta = ts && mTs ? Math.abs(mTs - ts) : Number.POSITIVE_INFINITY;
      if (score === "strict") {
        if (delta < bestStrictDelta) { bestStrictDelta = delta; bestStrict = m; }
      } else if (delta < bestLooseDelta) {
        bestLooseDelta = delta; bestLoose = m;
      }
    }
    return bestStrict ?? bestLoose;
  }

  return sortedLedger.map((it) => {
    const best = chooseMedia(it);
    if (best) usedMediaIds.add(best.id);
    return {
      key: `tx-${it.transaction_id}`,
      ofMessageId: best?.id ?? it.message_id,
      text: best ? stripOFHtml(best.text) : "",
      priceCents: it.price_cents,
      purchasedAt: it.purchased_at ?? it.sent_at,
      thumbs: best ? pickThumbs(best.media) : [],
      netCents: it.net_cents,
      feeCents: it.fee_cents,
      vatCents: it.vat_cents,
      employeeName: it.employee_name,
      employeeColor: it.employee_color,
    };
  });
}

function PpvHistoryChart({ items, maxCents }: { items: PpvHistoryItem[]; maxCents: number }) {
  // Geometry sized for the 360-wide drawer minus its 16px padding. The
  // viewBox + preserveAspectRatio=none lets the SVG scale fluidly if a
  // future wider variant of the drawer ships.
  const W = 320;
  const H = 120;
  const padL = 8;
  const padR = 8;
  const padT = 18;   // room for the $ label above the highest dot
  const padB = 14;   // room for the date axis
  const innerW = W - padL - padR;
  const innerH = H - padT - padB;
  const n = items.length;
  const slotW = n > 1 ? innerW / (n - 1) : 0;
  const yScale = maxCents > 0 ? innerH / maxCents : 0;

  const points = items.map((it, i) => {
    const x = n === 1 ? padL + innerW / 2 : padL + i * slotW;
    const y = padT + (innerH - it.price_cents * yScale);
    return { x, y, it };
  });

  const path = points.map((p, i) => `${i === 0 ? "M" : "L"} ${p.x.toFixed(1)} ${p.y.toFixed(1)}`).join(" ");

  const firstDate = items[0].purchased_at ?? items[0].sent_at;
  const lastDate = items[n - 1].purchased_at ?? items[n - 1].sent_at;

  return (
    <div className="relative">
      <svg
        viewBox={`0 0 ${W} ${H}`}
        preserveAspectRatio="none"
        className="w-full h-28"
        role="img"
        aria-label="Recent PPV purchases by this fan"
      >
        {/* Gridlines — 4 horizontal bands. Dashed, low-contrast. */}
        {[0, 0.25, 0.5, 0.75, 1].map((f) => {
          const y = padT + innerH * f;
          return (
            <line
              key={f}
              x1={padL}
              x2={W - padR}
              y1={y}
              y2={y}
              className="stroke-border"
              strokeDasharray="2 3"
              strokeWidth={0.5}
            />
          );
        })}
        {n > 1 && (
          <path d={path} className="stroke-accent" strokeWidth={1.5} fill="none" />
        )}
        {points.map((p, i) => (
          <g key={p.it.transaction_id}>
            <circle cx={p.x} cy={p.y} r={3} className="fill-accent">
              <title>
                {`${fmtUsd(p.it.price_cents)} — ${fmtRelTime(p.it.purchased_at ?? p.it.sent_at)}`}
              </title>
            </circle>
            {/* Inline price labels — only every other one if it gets crowded. */}
            {(n <= 8 || i % 2 === 0) && (
              <text
                x={p.x}
                y={Math.max(p.y - 6, 10)}
                textAnchor="middle"
                className="fill-fg-dim"
                style={{ fontSize: 9 }}
              >
                {fmtUsd(p.it.price_cents)}
              </text>
            )}
          </g>
        ))}
      </svg>
      <div className="flex justify-between text-[9px] text-fg-dim px-1 -mt-1">
        <span>{fmtChartDate(firstDate)}</span>
        <span>{fmtChartDate(lastDate)}</span>
      </div>
    </div>
  );
}

function fmtChartDate(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function sameList(a: string[], b: string[]): boolean {
  if (a.length !== b.length) return false;
  const A = a.slice().sort();
  const B = b.slice().sort();
  return A.every((v, i) => v === B[i]);
}

// Per-fan language override options. "Default (account)" is the empty value — it clears
// the override so the fan follows the account's language. Picking one pins it MANUAL,
// so gen_info's auto-detection can never change it.
const LANGUAGE_OPTIONS: { code: string; label: string }[] = [
  { code: "en", label: "English" },
  { code: "es", label: "Español" },
  { code: "sl", label: "Slovenščina" },
  { code: "pt", label: "Português" },
  { code: "fr", label: "Français" },
  { code: "de", label: "Deutsch" },
  { code: "it", label: "Italiano" },
];

function languageHint(language: string | null, source: string | null): string {
  if (!language) return "Using the account default. Pick one to pin this fan.";
  if (source === "manual") return "Manual override — auto-detection won't change it.";
  return "Auto-detected. Pick one to pin it (stops auto-detection).";
}

const LANG_LABEL: Record<string, string> = {
  en: "English", es: "Español", sl: "Slovenščina", pt: "Português",
  fr: "Français", de: "Deutsch", it: "Italiano",
};

function fmtCommStyle(cs: Record<string, unknown> | undefined): string {
  if (!cs) return "";
  return Object.entries(cs).filter(([, v]) => v === true).map(([k]) => k.replace(/_/g, " ")).join(", ");
}

// Read-only card summarising what gen_info learned: the detected language, the rich
// note, the extracted facts, a dated timeline, and the openers it wrote.
function ProfileCard({ fan }: { fan: FanRecord }) {
  const p = fan.profile;
  const facts: [string, string | null | undefined][] = [
    ["Age", fan.his_age],
    ["Location", [fan.home_city, fan.home_country].filter(Boolean).join(", ") || null],
    ["Job", fan.occupation],
    ["Employer", fan.employer],
    ["Relationship", fan.relationship_status],
    ["Stage", fan.relationship_stage],
    ["Kids", fan.has_kids == null ? null : fan.has_kids ? "yes" : "no"],
    ["Pets", Array.isArray(fan.pets) && fan.pets.length ? fan.pets.map(petLabel).join(", ") : null],
    ["Style", fmtCommStyle(fan.communication_style) || null],
  ];
  const shown = facts.filter(([, v]) => v);
  const timeline = (fan.recent_events_timeline ?? []).filter((t) => t?.event);
  const richNote = p?.rich_note?.trim();
  const langCode = fan.language ?? "";
  const nothing = !richNote && shown.length === 0 && timeline.length === 0 &&
    !(p?.q?.length) && !(p?.tease?.length);

  return (
    <div className="rounded-lg border border-border bg-bg-elev-1/40 p-3">
      <div className="flex items-center justify-between mb-2">
        <span className="text-[11px] font-medium text-fg-dim uppercase tracking-wide">AI profile</span>
        <div className="flex items-center gap-2">
          {langCode ? (
            <span className="text-[10px] px-1.5 py-0.5 rounded-full border border-border text-fg-dim"
              title={fan.language_source === "manual" ? "Manual override" : "Auto-detected"}>
              🌐 {LANG_LABEL[langCode] ?? langCode}
              {fan.language_source === "manual" ? " · pinned" : ""}
            </span>
          ) : null}
          {p?.last_generated_at ? (
            <span className="text-[10px] text-fg-dim">{fmtRelTime(p.last_generated_at)}</span>
          ) : null}
        </div>
      </div>

      {nothing ? (
        <div className="text-[11px] text-fg-dim">No profile yet — gen_info fills this once he chats a little.</div>
      ) : (
        <div className="flex flex-col gap-3">
          {richNote ? (
            <div className="text-[11px] text-fg whitespace-pre-line leading-relaxed">{richNote}</div>
          ) : null}

          {shown.length ? (
            <div className="grid grid-cols-2 gap-x-3 gap-y-1">
              {shown.map(([k, v]) => (
                <div key={k} className="text-[11px]">
                  <span className="text-fg-dim">{k}: </span><span className="text-fg">{v}</span>
                </div>
              ))}
            </div>
          ) : null}

          {timeline.length ? (
            <div>
              <div className="text-[10px] text-fg-dim uppercase tracking-wide mb-1">Timeline</div>
              <ul className="space-y-0.5">
                {timeline.slice(-6).reverse().map((t, i) => (
                  <li key={i} className="text-[11px] text-fg-dim">
                    <span className="font-mono text-[10px]">{t.date}</span> · <span className="text-fg">{t.event}</span>
                  </li>
                ))}
              </ul>
            </div>
          ) : null}

          {p?.q?.length || p?.tease?.length ? (
            <div className="text-[10px] text-fg-dim">
              {p?.q?.length ? <div>{p.q.length} question{p.q.length > 1 ? "s" : ""} + {p?.tease?.length ?? 0} tease{(p?.tease?.length ?? 0) > 1 ? "s" : ""} ready</div> : null}
            </div>
          ) : null}
        </div>
      )}
    </div>
  );
}

function petLabel(p: unknown): string {
  if (p && typeof p === "object") {
    const o = p as Record<string, unknown>;
    return [o.kind, o.name].filter(Boolean).join(" ") || JSON.stringify(o);
  }
  return String(p);
}

// OF stores both the per-fan rename and the private creator-side note on
// the subscription object. Both use PUT /subscriptions/{id} but the relay
// exposes them as separate endpoints. Empty string clears the field on OF.
// X-Account-Id (via the ctx) is required — without it the relay can't
// route the upstream call to the right account's OFClient.
function writeOFCustomName(accountId: string, fanId: FanId, name: string) {
  return relay.put(
    `/api/of/v2/subscriptions/${fanId}/custom-name`,
    { name },
    { accountId },
  );
}

function writeOFNotice(accountId: string, fanId: FanId, note: string) {
  return relay.put(
    `/api/of/v2/subscriptions/${fanId}/note`,
    { note },
    { accountId },
  );
}

const _cn = cn; void _cn; // ensure module is type-checked if we add cn later
