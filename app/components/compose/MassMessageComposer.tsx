"use client";

/**
 * MassMessageComposer — modal for broadcasting a single message to many
 * fans via POST /api/of/v2/messages/queue. With `scheduled_date` the
 * relay routes to schedule_mass_message; without it, send_mass_message
 * fires immediately.
 *
 * Audience mirrors the desktop app: include vs exclude columns. Include
 * accepts any combination of OF's five system audiences (fans / recent /
 * following / rebill_off / tagged) and any of the model's custom fan
 * lists. Exclude is custom-lists-only (OF doesn't accept system audience
 * names in excludeUserLists).
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { useActiveAccounts } from "@/hooks/useAccounts";
import { useAllModelsInclude } from "@/hooks/useAllModelsInclude";
import { useFunnels } from "@/hooks/useFunnels";
import { useEmployee } from "@/contexts/EmployeeContext";
import { relay, type VaultMedia } from "@/lib/relay";
import { VaultPicker } from "@/components/chat/VaultPicker";
import { EmojiPickerButton, EmojiQuickRow, insertAtCursor } from "@/components/chat/EmojiBar";
import { GifPickerButton, GifPickerStrip, type PickedGif } from "@/components/chat/GifPicker";
import { TemplatePicker, templateMediaToVault, type PickedTemplate } from "@/components/chat/TemplatePicker";
import { TagCreatorsPicker, TaggedCreatorChips, type TaggedCreatorChoice } from "@/components/chat/TagCreatorsPicker";
import { localDatetimeToIso, recordSchedule } from "@/lib/scheduleHistory";

import { sanitizePriceInput } from "@/components/chat/Composer";

import { AccountPicker } from "./AccountPicker";
import { MediaTray } from "./MediaTray";
import { ScheduleField } from "./ScheduleField";

interface FanOutResult {
  accountId: string;
  ok: boolean;
  error?: string;
}

function summarizeFanOut(results: FanOutResult[]): string {
  const ok = results.filter((r) => r.ok).length;
  return `${ok}/${results.length} succeeded`;
}

interface FanList {
  id: number | string;
  name?: string;
  /** OF tags lists as 'custom' or 'build-in' (sic) — case-spelling varies. */
  type?: string;
  usersCount?: number;
}
/** OF returns either `{list:[…]}` or a bare array depending on params. */
type ListsResp = { list?: FanList[]; hasMore?: boolean } | FanList[];

/** OF-recognised virtual audience IDs. The /lists endpoint does NOT
 *  return these — they're hardcoded server-side names that `userLists`
 *  accepts. We expose two OF guarantees on every account: All fans and
 *  Following. The desktop app advertises five (adding `recent`, `online`,
 *  `tagged`) but those depend on per-account state and `online` was
 *  intentionally dropped on user request — the "currently online" filter
 *  isn't actionable for scheduled / mass sends. */
const SYSTEM_AUDIENCES: Array<{ id: string; label: string }> = [
  { id: "fans",      label: "All fans"  },
  { id: "following", label: "Following" },
];

/** Names that should float to the top of the custom-list columns, in
 *  this priority order. Exact lowercase match only — a list named
 *  "fans" sorts first, but variations like "allFans" / "All fans"
 *  stay in alphabetical so they don't crowd the top. */
const TOP_LIST_NAMES: string[] = [
  "fans",
  "following",
];

function topRank(name: string): number {
  const i = TOP_LIST_NAMES.indexOf(name.toLowerCase());
  return i === -1 ? TOP_LIST_NAMES.length : i;
}

export function MassMessageComposer({
  open,
  onClose,
  mode = "standard",
  forcedAccountId,
  onComposePayload,
}: {
  open: boolean;
  onClose: () => void;
  /** "online" reveals the live-targeting panel (online-only, unread, recent-
   *  chat include, recent-activity excludes, auto-unsend) and titles the modal
   *  "Mass Online". "standard" is the classic list-based broadcast. */
  mode?: "standard" | "online";
  /** Return-payload mode: pin the account (a rule is single-account) and hide
   *  the picker + all-models switch + schedule field. */
  forcedAccountId?: string;
  /** Return-payload mode: instead of POSTing the broadcast, hand the built
   *  send_mass_message payload back so the RuleEditor can store it on a rule.
   *  The default (no callback) send path is untouched. */
  onComposePayload?: (payload: Record<string, unknown>) => void;
}) {
  const online = mode === "online";
  const composeMode = !!onComposePayload;
  const qc = useQueryClient();
  const activeAccounts = useActiveAccounts();
  // Shared include set with ScopeSwitcher's all-models aggregate — a model
  // un-checked there is also omitted from a broadcast here.
  const { isIncluded, toggle: toggleAllModelsInclude } = useAllModelsInclude();
  // Sends without an explicit employeeId attribute to the Automation system
  // employee (id=2). See memory: project_x_employee_id_pipeline.md — relay's
  // X-Employee-Id header is opt-in per call, no global injector.
  const { current: currentEmployee } = useEmployee();
  const [allModels, setAllModels] = useState(false);
  const [accountId, setAccountId] = useState<string | null>(forcedAccountId ?? null);
  const [text, setText] = useState("");
  // Vault-picked media for single-account broadcasts. All-models mode is
  // text-only for now (upload-from-computer is paused).
  const [attached, setAttached] = useState<VaultMedia[]>([]);
  const [price, setPrice] = useState<string>("");
  // Leading N tiles are FREE previews; rest are PPV-locked. Reordering a
  // tile across the divider is the primary way to change preview vs paid.
  // Local for now — wire-payload plumbing lives in ask #8 (Work Unit O).
  const [previewCount, setPreviewCount] = useState<number>(0);
  const [lockedText, setLockedText] = useState(false);
  // Default audience: blast fans + following. The "online" exclude was
  // dropped (ask 2026-05-23) — the system "online" preset is point-in-time
  // and not useful for scheduled or large broadcasts.
  const [includes, setIncludes] = useState<Set<string>>(() => new Set(["fans", "following"]));
  const [excludes, setExcludes] = useState<Set<string>>(() => new Set());
  const [schedule, setSchedule] = useState<string>("");
  // Optional funnel: when set, the send_mass_message automation walks this
  // funnel's reply/PPV steps per replying fan after the opener lands. Resolved
  // server-side by funnel_id (send_mass_message._resolve_funnel) — only the
  // opener (this message) is sent now. Inert on the live OF-queue path.
  const [funnelId, setFunnelId] = useState<number | null>(null);
  const funnelsQ = useFunnels();
  const [pickerOpen, setPickerOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [progress, setProgress] = useState<string | null>(null);
  const [results, setResults] = useState<FanOutResult[] | null>(null);
  const [taggedCreators, setTaggedCreators] = useState<TaggedCreatorChoice[]>([]);
  // Picked GIFs. OF's /messages/queue accepts top-level `giphyId` (verified
  // live 2026-05-23). One GIF per broadcast; extra picks fan out as
  // standalone GIF-only broadcasts after the primary, same pattern the
  // chat composer uses for direct sends.
  const [pickedGifs, setPickedGifs] = useState<PickedGif[]>([]);
  const [gifPickerOpen, setGifPickerOpen] = useState(false);
  // ── Online / live-targeting (mode === "online") ──────────────────────
  // These map 1:1 to the new /messages/queue body fields the relay resolves
  // server-side. Kept as strings so the inputs can be blank (= "not applied").
  const [onlineOnly, setOnlineOnly] = useState(true);
  const [unreadLimit, setUnreadLimit] = useState("");        // → unread_limit
  const [recentChatHours, setRecentChatHours] = useState(""); // → recent_chat_hours
  const [recentChatLimit, setRecentChatLimit] = useState(""); // → recent_chat_limit
  const [excludeRepliedHours, setExcludeRepliedHours] = useState(""); // → exclude_replied_hours
  const [excludeInboundHours, setExcludeInboundHours] = useState(""); // → exclude_inbound_hours
  const [unsendAfterHours, setUnsendAfterHours] = useState(""); // → unsend_after_hours
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);

  const listsQ = useQuery<FanList[]>({
    queryKey: ["fan-lists", accountId ?? ""],
    enabled: !!accountId,
    staleTime: 5 * 60_000,
    queryFn: async () => {
      const r = await relay.get<ListsResp>(
        "/api/of/v2/lists?limit=50",
        { accountId: accountId! },
      );
      return Array.isArray(r) ? r : (r?.list ?? []);
    },
  });

  const customLists = useMemo(() => {
    const filtered = (listsQ.data ?? []).filter(
      (l) => l.type !== "build-in" && l.type !== "built-in",
    );
    // Sort: Fans → Following → Online first (mirrors system-audience
    // order), then alphabetical for everything else. Catches lists the
    // user manually re-created under those names too.
    return filtered.slice().sort((a, b) => {
      const an = (a.name ?? "").toLowerCase();
      const bn = (b.name ?? "").toLowerCase();
      const ra = topRank(an);
      const rb = topRank(bn);
      if (ra !== rb) return ra - rb;
      return an.localeCompare(bn);
    });
  }, [listsQ.data]);

  // A list shouldn't be in include AND exclude simultaneously — toggling
  // one auto-removes the other to keep the UI consistent.
  function toggleInclude(id: string) {
    setIncludes((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
    setExcludes((prev) => {
      if (!prev.has(id)) return prev;
      const next = new Set(prev);
      next.delete(id);
      return next;
    });
  }
  function toggleExclude(id: string) {
    setExcludes((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
    setIncludes((prev) => {
      if (!prev.has(id)) return prev;
      const next = new Set(prev);
      next.delete(id);
      return next;
    });
  }

  /** System-audience chips are tri-state: off → include → exclude → off.
   *  Lets the user say "All fans BUT NOT Following" with one click each
   *  instead of needing two parallel chip strips. */
  function cycleSystem(id: string) {
    if (includes.has(id)) {
      // include → exclude
      setIncludes((prev) => {
        const next = new Set(prev); next.delete(id); return next;
      });
      setExcludes((prev) => new Set(prev).add(id));
    } else if (excludes.has(id)) {
      // exclude → off
      setExcludes((prev) => {
        const next = new Set(prev); next.delete(id); return next;
      });
    } else {
      // off → include
      setIncludes((prev) => new Set(prev).add(id));
    }
  }

  // When the account changes, re-seed audience to system defaults — the prior
  // set was scoped to a different model's custom lists.
  useEffect(() => {
    setIncludes(new Set(["fans", "following"]));
    setExcludes(new Set());
  }, [accountId]);

  // All-models mode tosses custom-list selections + picked vault media + any
  // price/lock state (broadcasts force free per ask #4). Audience falls back
  // to system defaults which are identical across every account.
  useEffect(() => {
    if (allModels) {
      setAccountId(null);
      setAttached([]);
      setPrice("");
      setLockedText(false);
      setPreviewCount(0);
      // Tagged-friend lists are per-account on OF — clearing avoids
      // sending tags valid for one model but rejected by the rest.
      setTaggedCreators([]);
    }
    setIncludes(new Set(["fans", "following"]));
    setExcludes(new Set());
  }, [allModels]);

  // All-models broadcasts force free + unlocked-text (ask #4). For per-employee
  // sends, a typed price > 0 is the sole paid signal — clearing the field is
  // how the user opts back into a free send.
  const numericPrice = price ? Number(price) : 0;
  const effectivePrice = allModels ? 0 : numericPrice;
  const effectiveLockedText = allModels ? false : lockedText;

  // No auto-seed: 0 previews is a valid choice on paid broadcasts (every
  // tile locked). The ± stepper lets the user opt into a leading free
  // teaser when they want one.

  // Honour the shared include set from ScopeSwitcher: models the user
  // unchecked there are also dropped from the broadcast fan-out.
  const broadcastAccounts = useMemo(
    () => activeAccounts.filter((a) => isIncluded(a.id)),
    [activeAccounts, isIncluded],
  );
  const allAccountIds = useMemo(() => activeAccounts.map((a) => a.id), [activeAccounts]);

  // Build the mass-send body from the current form. Shared by the live send
  // path (submitOne → POST) and the return-payload path (buildComposePayload).
  // The key set matches what the send_mass_message AUTOMATION reads, so the same
  // object works as a rule payload.
  function buildBody(
    mediaFiles: Array<number | Record<string, unknown>>,
    scheduledIso: string | null,
    giphyId: string | null,
  ): Record<string, unknown> {
    // Numeric vault ids only — fresh-claim dicts have no id until OF
    // resolves them post-send, so they can't appear in `previews`. Match
    // the order of mediaFiles so OF's id correspondence stays consistent.
    const previewIds: number[] = effectivePrice > 0 && previewCount > 0
      ? mediaFiles
          .slice(0, previewCount)
          .filter((m): m is number => typeof m === "number" && m > 0)
      : [];
    const body: Record<string, unknown> = {
      text: text.trim(),
      scheduled_date: scheduledIso,
      user_lists: Array.from(includes),
      user_ids: [],
      excluded_users: [],
      excluded_user_lists: Array.from(excludes),
      price: effectivePrice,
      locked_text: effectiveLockedText && effectivePrice > 0,
      media_files: mediaFiles,
    };
    if (previewIds.length > 0) body.previews = previewIds;
    if (taggedCreators.length > 0) {
      body.tagged_users = taggedCreators.map((c) => c.id);
    }
    if (funnelId) body.funnel_id = funnelId;
    if (giphyId) body.giphy_id = giphyId;
    // Online / live-targeting fields. Only attach what the operator set so the
    // relay leaves unused filters untouched. A blank input = "not applied".
    if (online) {
      const num = (s: string): number | null => {
        const n = Number(s);
        return s.trim() !== "" && Number.isFinite(n) && n > 0 ? n : null;
      };
      if (onlineOnly) body.online_only = true;
      const ul = num(unreadLimit);
      if (ul) body.unread_limit = Math.floor(ul);
      const rch = num(recentChatHours);
      if (rch) body.recent_chat_hours = rch;
      const rcl = num(recentChatLimit);
      if (rcl) body.recent_chat_limit = Math.floor(rcl);
      const erh = num(excludeRepliedHours);
      if (erh) body.exclude_replied_hours = erh;
      const eih = num(excludeInboundHours);
      if (eih) body.exclude_inbound_hours = eih;
      const uah = num(unsendAfterHours);
      if (uah) body.unsend_after_hours = uah;
    }
    return body;
  }

  // Shared per-account submit: builds the body, POSTs, optionally records
  // the schedule pick on success.
  async function submitOne(
    forAccountId: string,
    mediaFiles: Array<number | Record<string, unknown>>,
    scheduledIso: string | null,
    giphyId: string | null,
  ) {
    const body = buildBody(mediaFiles, scheduledIso, giphyId);
    const resp = await relay.post(
      "/api/of/v2/messages/queue",
      body,
      { accountId: forAccountId, employeeId: currentEmployee?.id },
    );
    if (schedule) recordSchedule(forAccountId, schedule);
    return resp;
  }

  /** Return-payload mode: build the send_mass_message rule payload (vault ids
   *  only, no schedule) and hand it back — never POSTs. Returns null + sets an
   *  error when there's nothing usable to send. */
  function useInRule() {
    setError(null);
    const trimmed = text.trim();
    const mediaFiles = attached
      .map((m) => m.id)
      .filter((id): id is number => typeof id === "number" && id > 0);
    if (!trimmed && mediaFiles.length === 0 && pickedGifs.length === 0) {
      setError("Add text, media, or a GIF");
      return;
    }
    if (includes.size === 0) {
      setError("Pick at least one audience list to include");
      return;
    }
    if (!Number.isFinite(effectivePrice) || effectivePrice < 0) {
      setError("Invalid price");
      return;
    }
    const body = buildBody(mediaFiles, null, pickedGifs[0]?.id ?? null);
    delete body.scheduled_date; // the rule's cadence schedules it, not this
    onComposePayload?.(body);
  }

  const send = useMutation({
    mutationFn: async () => {
      const trimmed = text.trim();
      if (allModels) {
        if (broadcastAccounts.length === 0) throw new Error("No active models");
      } else {
        if (!accountId) throw new Error("Pick an account");
      }
      const totalMedia = allModels ? 0 : attached.length;
      const hasGif = pickedGifs.length > 0;
      if (!trimmed && totalMedia === 0 && !hasGif) throw new Error("Add text, media, or a GIF");
      if (includes.size === 0) throw new Error("Pick at least one audience list to include");
      if (!Number.isFinite(effectivePrice) || effectivePrice < 0) throw new Error("Invalid price");
      const scheduledIso = schedule ? localDatetimeToIso(schedule) : null;
      if (schedule && !scheduledIso) throw new Error("Invalid schedule date");

      // OF accepts ONE GIF per send. When the user picked multiple, the
      // first rides this broadcast; the rest fan out as standalone GIF-only
      // sends after the main one returns — same convention as the chat
      // composer.
      const firstGif = pickedGifs[0]?.id ?? null;
      const trailingGifs = pickedGifs.slice(1);

      if (allModels) {
        // Text-only fan-out — each included account broadcasts the same
        // body sequentially. No media until upload-from-computer comes back.
        const accountIds = broadcastAccounts.map((a) => a.id);
        setProgress(`Sending to 0 / ${accountIds.length}…`);
        const fanResults: FanOutResult[] = [];
        for (let i = 0; i < accountIds.length; i++) {
          const aid = accountIds[i];
          try {
            await submitOne(aid, [], scheduledIso, firstGif);
            fanResults.push({ accountId: aid, ok: true });
          } catch (e) {
            fanResults.push({ accountId: aid, ok: false, error: (e as Error).message });
          }
          setProgress(`Sending to ${i + 1} / ${accountIds.length}…`);
        }
        setResults(fanResults);
        setProgress(null);
        if (fanResults.every((r) => r.ok)) return fanResults;
        throw new Error(summarizeFanOut(fanResults));
      }

      // Single-account path: mixed payload (vault ids + fresh claims).
      const mediaFiles: Array<number | Record<string, unknown>> = attached.map(
        (m) => m._claim ?? m.id,
      );
      const primary = await submitOne(accountId!, mediaFiles, scheduledIso, firstGif);
      // Fan out remaining GIFs as text/media-less GIF-only broadcasts so
      // they all land in the same audience without OF rejecting multi-GIF.
      for (const g of trailingGifs) {
        try {
          await submitOne(accountId!, [], scheduledIso, g.id);
        } catch (e) {
          console.warn("[mass] trailing GIF send failed", e);
        }
      }
      return primary;
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["messages-queue"] });
      reset();
      onClose();
    },
    onError: (err: Error) => {
      setProgress(null);
      setError(err.message);
    },
  });

  function reset() {
    setText("");
    setAttached([]);
    setPrice("");
    setPreviewCount(0);
    setLockedText(false);
    setIncludes(new Set(["fans", "following"]));
    setExcludes(new Set());
    setSchedule("");
    setFunnelId(null);
    setError(null);
    setProgress(null);
    setResults(null);
    setAllModels(false);
    setTaggedCreators([]);
    setPickedGifs([]);
    setGifPickerOpen(false);
    setOnlineOnly(true);
    setUnreadLimit("");
    setRecentChatHours("");
    setRecentChatLimit("");
    setExcludeRepliedHours("");
    setExcludeInboundHours("");
    setUnsendAfterHours("");
  }

  function onEmoji(em: string) {
    const ta = textareaRef.current;
    if (!ta) {
      setText((t) => t + em);
      return;
    }
    insertAtCursor(ta, text, em, setText);
  }

  function onPickTemplate(t: PickedTemplate) {
    // Snapshot semantics — replace attachments and preview-count
    // wholesale so the saved FREE-first ordering survives the pick.
    setText(t.text);
    setAttached(templateMediaToVault(t));
    if (t.price > 0) {
      setPrice(String(t.price));
      setLockedText(t.lockedText);
    }
    setPreviewCount(t.previews.length);
    if (t.taggedUsers.length > 0) {
      setTaggedCreators((prev) => {
        const have = new Set(prev.map((c) => c.id));
        const additions: TaggedCreatorChoice[] = t.taggedUsers
          .filter((id) => !have.has(id))
          .map((id) => ({ id, name: `user${id}`, username: "", avatar: null }));
        return [...prev, ...additions];
      });
    }
    // GIF-only templates seed the picked-GIF state so the broadcast carries
    // the GIF without the user needing to re-pick it.
    if (t.gifId) {
      const url = t.gifUrl ?? "";
      setPickedGifs([{ id: t.gifId, thumbUrl: url, fullUrl: url, title: t.title }]);
    }
    setTimeout(() => textareaRef.current?.focus(), 0);
  }

  if (!open) return null;

  const isPPV = effectivePrice > 0;
  const isScheduled = !!schedule;

  // Reach preview — sum the user-count of the custom lists we're including
  // (system audiences don't expose counts, so we hint them separately).
  const includeReach = Array.from(includes).reduce((sum, id) => {
    const l = customLists.find((x) => String(x.id) === id);
    return sum + (l?.usersCount ?? 0);
  }, 0);
  const hasSystem = Array.from(includes).some((id) => SYSTEM_AUDIENCES.some((b) => b.id === id));

  return (
    <div
      className="fixed inset-0 z-50 bg-black/50 grid place-items-center p-4"
      onClick={() => { if (!send.isPending) { reset(); onClose(); } }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="w-full max-w-[640px] max-h-[90vh] flex flex-col bg-panel border border-border rounded-xl shadow-2xl"
      >
        <header className="px-4 py-3 border-b border-border flex items-center justify-between">
          <h2 className="text-sm font-semibold">
            {composeMode
              ? "Compose mass message for rule"
              : online ? "New Mass Online message" : "New mass message"}
          </h2>
          <button
            type="button"
            onClick={() => { if (!send.isPending) { reset(); onClose(); } }}
            className="text-fg-dim hover:text-fg text-lg leading-none"
            title="Close"
          >×</button>
        </header>

        <div className="flex-1 overflow-y-auto p-4 space-y-3">
          {/* All-models switch — pivots the modal between single-account
           *  mode (vault + custom-list audiences) and fan-out mode
           *  (uploads only + system audiences only). Hidden in return-payload
           *  mode: a rule targets exactly one account (forcedAccountId). */}
          {!composeMode && (
            <label className="flex items-center gap-2 text-xs cursor-pointer select-none">
              <input
                type="checkbox"
                checked={allModels}
                onChange={(e) => setAllModels(e.target.checked)}
              />
              <span className="font-medium">Broadcast from ALL models</span>
              <span className="text-fg-dim">
                ({broadcastAccounts.length}/{activeAccounts.length} included)
              </span>
            </label>
          )}

          {composeMode ? null : allModels ? (
            <>
              <div className="text-[11px] text-fg-dim border border-dashed border-border rounded-md py-2 px-3">
                All-models broadcasts are text-only for now — uploads from
                computer are temporarily disabled.
              </div>
              {/* Chip strip — mirrors ScopeSwitcher's include set. Click a
               *  chip to drop / re-add a model from the broadcast (and
               *  from the all-models aggregate everywhere else). */}
              <div className="flex flex-wrap gap-1.5">
                {activeAccounts.map((a) => {
                  const inc = isIncluded(a.id);
                  return (
                    <button
                      key={a.id}
                      type="button"
                      onClick={() => toggleAllModelsInclude(a.id, allAccountIds)}
                      title={inc ? "Click to exclude from broadcast" : "Click to include"}
                      className={
                        "flex items-center gap-1.5 px-2 py-1 rounded-full text-[11px] border transition-colors " +
                        (inc
                          ? "bg-bg-elev-1 border-border text-fg"
                          : "bg-bg border-border/60 text-fg-dim line-through opacity-60")
                      }
                    >
                      <span
                        className="w-2 h-2 rounded-full shrink-0"
                        style={{ background: a.color || "#666" }}
                      />
                      <span>{a.nickname || a.id}</span>
                    </button>
                  );
                })}
                {activeAccounts.length === 0 && (
                  <span className="text-[11px] text-fg-dim">No active sessions.</span>
                )}
              </div>
            </>
          ) : (
            <AccountPicker value={accountId} onChange={setAccountId} />
          )}

          <div className="space-y-1.5">
            <textarea
              ref={textareaRef}
              value={text}
              onChange={(e) => setText(e.target.value)}
              placeholder="What's the broadcast?"
              rows={4}
              className="w-full bg-bg border border-border rounded-md px-3 py-2 text-sm placeholder:text-muted focus:outline-none focus:border-accent resize-y"
            />
            {/* Toolbar — same composer features as the chat: recent-
             *  emojis quick row, full emoji picker, GIF picker,
             *  saved-template picker, @-tag creators picker. */}
            <div className="flex items-center gap-1.5 flex-wrap">
              <EmojiQuickRow onInsert={onEmoji} />
              <EmojiPickerButton align="left" onInsert={onEmoji} />
              <GifPickerButton
                open={gifPickerOpen}
                onToggle={() => setGifPickerOpen((v) => !v)}
              />
              <TemplatePicker
                accountId={accountId}
                onPick={onPickTemplate}
                hideImageTemplates
              />
              {!allModels && (
                <TagCreatorsPicker
                  accountId={accountId}
                  selected={taggedCreators}
                  onChange={setTaggedCreators}
                />
              )}
            </div>
            {gifPickerOpen && (
              <GifPickerStrip
                accountId={accountId}
                onClose={() => setGifPickerOpen(false)}
                onPick={(g) => {
                  setPickedGifs((prev) =>
                    prev.some((p) => p.id === g.id) ? prev : [...prev, g],
                  );
                  setGifPickerOpen(false);
                }}
              />
            )}
            {pickedGifs.length > 0 && (
              <div className="flex items-center gap-1.5 flex-wrap bg-bg/60 border border-border rounded-md px-2 py-2">
                <span className="text-[10px] uppercase tracking-wide text-fg-dim mr-1">gifs</span>
                {pickedGifs.map((g) => (
                  <div key={g.id} className="relative w-14 h-14 rounded-md overflow-hidden border border-border bg-bg-elev-1">
                    {/* eslint-disable-next-line @next/next/no-img-element */}
                    <img src={g.thumbUrl} alt={g.title || "GIF"} className="w-full h-full object-cover" />
                    <button
                      type="button"
                      onClick={() => setPickedGifs((prev) => prev.filter((p) => p.id !== g.id))}
                      className="absolute top-0.5 right-0.5 w-4 h-4 rounded-full bg-black/70 text-white grid place-items-center text-[10px] opacity-80 hover:opacity-100"
                      aria-label="Remove GIF"
                    >
                      ✕
                    </button>
                  </div>
                ))}
                <span className="ml-auto text-[10px] text-fg-dim">
                  {pickedGifs.length > 1
                    ? `${pickedGifs.length} GIFs — first rides this broadcast; rest fan out as standalone sends`
                    : "1 GIF attached"}
                </span>
              </div>
            )}
            {!allModels && taggedCreators.length > 0 && (
              <TaggedCreatorChips selected={taggedCreators} onChange={setTaggedCreators} />
            )}
          </div>

          {!allModels && (
            <MediaTray
              accountId={accountId}
              attached={attached}
              onChange={setAttached}
              onOpenVaultPicker={() => setPickerOpen(true)}
              price={effectivePrice}
              previewCount={previewCount}
              onPreviewCountChange={setPreviewCount}
            />
          )}

          {!allModels && (
            <>
              <div className="flex items-center gap-2">
                <label className="text-xs text-fg-dim shrink-0">Price (USD):</label>
                <input
                  type="text"
                  inputMode="decimal"
                  value={price}
                  onChange={(e) => setPrice(sanitizePriceInput(e.target.value))}
                  placeholder="0 (free)"
                  className="flex-1 bg-bg border border-border rounded-md px-2 py-1.5 text-xs focus:outline-none focus:border-accent"
                />
                <span className={isPPV ? "text-warn text-[11px]" : "text-fg-dim text-[11px]"}>
                  {isPPV ? `🔒 PPV $${effectivePrice.toFixed(2)}` : "free"}
                </span>
              </div>
              {isPPV && (
                <label className="flex items-center gap-2 text-xs text-fg-dim cursor-pointer">
                  <input
                    type="checkbox"
                    checked={lockedText}
                    onChange={(e) => setLockedText(e.target.checked)}
                  />
                  Lock the text too (fan must pay to read)
                </label>
              )}
            </>
          )}

          {/* Scheduling is the rule's job (its cadence), so hide it in
           *  return-payload mode. */}
          {!composeMode && (
            <ScheduleField
              scope={allModels ? "all-models" : accountId}
              value={schedule}
              onChange={setSchedule}
            />
          )}

          {/* Funnel — optional automated follow-up. Only the send_mass_message
           *  automation (compose-into-rule path) walks the steps; a one-off
           *  live OF-queue broadcast ignores it. */}
          <div className="flex items-center gap-2">
            <label className="text-xs text-fg-dim shrink-0">Funnel follow-up:</label>
            <select
              value={funnelId ?? ""}
              onChange={(e) => setFunnelId(e.target.value ? Number(e.target.value) : null)}
              className="flex-1 bg-bg border border-border rounded-md px-2 py-1.5 text-xs focus:outline-none focus:border-accent"
            >
              <option value="">— none (just send the opener) —</option>
              {(funnelsQ.data ?? []).map((f) => (
                <option key={f.id} value={f.id}>
                  {f.name} ({f.step_count} step{f.step_count === 1 ? "" : "s"})
                </option>
              ))}
            </select>
          </div>
          {funnelId != null && (
            <div className="text-[10px] text-fg-dim -mt-1">
              After this opener lands, the funnel walks its reply / PPV steps per
              replying fan (resolved by the automation, not this live send).
            </div>
          )}

          {/* Audience picker. In all-models mode we only show system
           *  audiences — custom lists are per-account so there's no
           *  list-id that means the same thing across every model. */}
          <div className="border border-border rounded-md p-3 space-y-3">
            <div className="text-[11px] uppercase tracking-wide text-fg-dim">
              Audience <span className="text-err">*</span>
            </div>
            <div className="space-y-1.5">
              <div className="flex flex-wrap gap-1.5">
                {SYSTEM_AUDIENCES.map((b) => {
                  const state = includes.has(b.id)
                    ? "in"
                    : excludes.has(b.id) ? "ex" : "off";
                  return (
                    <SysChip
                      key={b.id}
                      state={state}
                      onClick={() => cycleSystem(b.id)}
                      label={b.label}
                    />
                  );
                })}
              </div>
              <div className="text-[10px] text-fg-dim">
                Default: fans + following both included (click to change).
                Cycle: off → +include → −exclude → off.
              </div>
            </div>
            {!allModels && (
              <div className="grid grid-cols-2 gap-3 pt-2 border-t border-border/60">
                <div>
                  <div className="text-[11px] text-fg-dim mb-1.5">Custom lists — include</div>
                  <ListColumn
                    lists={customLists}
                    loading={listsQ.isLoading}
                    selected={includes}
                    onToggle={toggleInclude}
                    emptyLabel="No custom lists."
                  />
                </div>
                <div>
                  <div className="text-[11px] text-fg-dim mb-1.5">Custom lists — exclude</div>
                  <ListColumn
                    lists={customLists}
                    loading={listsQ.isLoading}
                    selected={excludes}
                    onToggle={toggleExclude}
                    emptyLabel="—"
                  />
                </div>
              </div>
            )}
            {!allModels && (includes.size > 0 || excludes.size > 0) && (
              <div className="text-[11px] text-fg-dim border-t border-border/60 pt-2">
                Reach: {includeReach} fan{includeReach === 1 ? "" : "s"} from custom
                {hasSystem ? " + system audience" : ""}
                {excludes.size > 0 && ` — minus ${excludes.size} list${excludes.size === 1 ? "" : "s"}`}
              </div>
            )}
          </div>

          {/* Online / live-targeting panel — only in "Mass Online" mode. These
           *  resolve server-side: online_only + the chosen audience list = fans
           *  online now; unread/recent ADD fans; the excludes drop recently-
           *  touched fans; auto-unsend schedules a delayed DELETE. */}
          {online && (
            <div className="border border-accent/30 bg-accent/5 rounded-md p-3 space-y-3">
              <div className="text-[11px] uppercase tracking-wide text-accent">
                Online targeting
              </div>

              <label className="flex items-center gap-2 text-xs cursor-pointer select-none">
                <input
                  type="checkbox"
                  checked={onlineOnly}
                  onChange={(e) => setOnlineOnly(e.target.checked)}
                />
                <span className="font-medium">Only fans online right now</span>
                <span className="text-fg-dim">(OF resolves at send time)</span>
              </label>

              <div className="grid grid-cols-2 gap-2">
                <NumField
                  label="Include fans with unread msgs"
                  suffix="up to N"
                  value={unreadLimit}
                  onChange={setUnreadLimit}
                  placeholder="e.g. 100"
                />
                <NumField
                  label="Include chatted-with"
                  suffix="last N hrs"
                  value={recentChatHours}
                  onChange={setRecentChatHours}
                  placeholder="e.g. 2"
                />
                <NumField
                  label="…cap recent-chat to"
                  suffix="N fans"
                  value={recentChatLimit}
                  onChange={setRecentChatLimit}
                  placeholder="(optional)"
                />
                <div />
                <NumField
                  label="Exclude I replied to"
                  suffix="last N hrs"
                  value={excludeRepliedHours}
                  onChange={setExcludeRepliedHours}
                  placeholder="e.g. 2"
                />
                <NumField
                  label="Exclude who messaged me"
                  suffix="last N hrs"
                  value={excludeInboundHours}
                  onChange={setExcludeInboundHours}
                  placeholder="e.g. 2"
                />
              </div>

              <div className="pt-2 border-t border-accent/20">
                <NumField
                  label="Auto-unsend this broadcast after"
                  suffix="N hrs"
                  value={unsendAfterHours}
                  onChange={setUnsendAfterHours}
                  placeholder="leave blank to keep it"
                />
                {isScheduled && unsendAfterHours.trim() !== "" && (
                  <div className="text-[10px] text-warn mt-1">
                    Auto-unsend applies to immediate sends only — ignored when scheduled.
                  </div>
                )}
              </div>

              <div className="text-[10px] text-fg-dim leading-relaxed">
                Audience = the lists above, filtered to online{onlineOnly ? "" : " (off)"}.
                Unread / chatted-with ADD fans; the two excludes remove fans you
                touched recently. Counts are resolved by the server at send time.
              </div>
            </div>
          )}

          {/* Per-account fan-out summary, shown after a partial-success run. */}
          {results && (
            <div className="border border-border rounded-md p-3 space-y-1 text-[11px]">
              <div className="font-medium">Fan-out results — {summarizeFanOut(results)}</div>
              {results.map((r) => (
                <div key={r.accountId} className={r.ok ? "text-ok" : "text-err"}>
                  {r.ok ? "✓" : "✗"} {r.accountId}
                  {r.error ? ` — ${r.error}` : ""}
                </div>
              ))}
            </div>
          )}
        </div>

        <footer className="px-4 py-3 border-t border-border flex items-center justify-end gap-2">
          {progress && <span className="text-fg-dim text-[11px] mr-auto">{progress}</span>}
          {error && !progress && <span className="text-err text-[11px] mr-auto">{error}</span>}
          <button
            type="button"
            onClick={() => { reset(); onClose(); }}
            disabled={send.isPending}
            className="text-xs px-3 py-1.5 rounded border border-border hover:border-border-light text-fg-dim hover:text-fg disabled:opacity-50"
          >
            Cancel
          </button>
          {composeMode ? (
            <button
              type="button"
              onClick={useInRule}
              disabled={includes.size === 0}
              className="text-xs px-4 py-1.5 rounded bg-accent text-white font-medium hover:bg-accent-hover disabled:opacity-50"
            >
              Use in rule
            </button>
          ) : (
            <button
              type="button"
              onClick={() => {
                setError(null);
                setResults(null);
                const action = isScheduled ? "Schedule" : "Broadcast";
                const target = allModels
                  ? `${broadcastAccounts.length} model${broadcastAccounts.length === 1 ? "" : "s"}`
                  : `${includes.size} audience group${includes.size === 1 ? "" : "s"}`;
                if (!confirm(`${action} from ${target}?`)) return;
                send.mutate();
              }}
              disabled={
                send.isPending ||
                includes.size === 0 ||
                (allModels ? broadcastAccounts.length === 0 : !accountId)
              }
              className="text-xs px-4 py-1.5 rounded bg-accent text-white font-medium hover:bg-accent-hover disabled:opacity-50"
            >
              {send.isPending
                ? (isScheduled ? "Scheduling…" : "Broadcasting…")
                : (isScheduled ? "Schedule" : "Broadcast")}
            </button>
          )}
        </footer>
      </div>

      <VaultPicker
        open={pickerOpen}
        onClose={() => setPickerOpen(false)}
        accountId={accountId}
        fanId={null}
        initialSelectedIds={attached.map((m) => m.id)}
        onConfirm={(p) => {
          setAttached(p);
          setPickerOpen(false);
        }}
      />
    </div>
  );
}

function ListColumn({
  lists, loading, selected, onToggle, emptyLabel,
}: {
  lists: FanList[];
  loading: boolean;
  selected: Set<string>;
  onToggle: (id: string) => void;
  emptyLabel: string;
}) {
  if (loading) return <div className="text-[11px] text-fg-dim">Loading…</div>;
  if (lists.length === 0) {
    return <div className="text-[11px] text-fg-dim italic">{emptyLabel}</div>;
  }
  return (
    <div className="flex flex-col gap-1 max-h-32 overflow-y-auto pr-1">
      {lists.map((l) => {
        const id = String(l.id);
        const checked = selected.has(id);
        return (
          <label
            key={id}
            className="flex items-center gap-1.5 text-xs cursor-pointer hover:bg-bg-elev-1/40 rounded px-1 py-0.5"
          >
            <input
              type="checkbox"
              checked={checked}
              onChange={() => onToggle(id)}
            />
            <span className="flex-1 truncate">{l.name || `List #${id}`}</span>
            <span className="text-[10px] text-fg-dim shrink-0">({l.usersCount ?? 0})</span>
          </label>
        );
      })}
    </div>
  );
}

/** Compact labelled numeric input for the Online-targeting panel. Blank = the
 *  filter isn't applied; the parent only forwards positive numbers. */
function NumField({
  label, suffix, value, onChange, placeholder,
}: {
  label: string;
  suffix: string;
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
}) {
  return (
    <label className="flex flex-col gap-1 text-[11px] text-fg-dim">
      <span className="leading-tight">{label} <span className="opacity-60">({suffix})</span></span>
      <input
        type="text"
        inputMode="numeric"
        value={value}
        onChange={(e) => onChange(e.target.value.replace(/[^0-9.]/g, ""))}
        placeholder={placeholder}
        className="bg-bg border border-border rounded-md px-2 py-1.5 text-xs text-fg focus:outline-none focus:border-accent"
      />
    </label>
  );
}

/** Tri-state chip for system audiences: off / include (+) / exclude (−). */
function SysChip({
  state, onClick, label,
}: {
  state: "off" | "in" | "ex";
  onClick: () => void;
  label: string;
}) {
  const stateStyles = {
    off: "bg-transparent text-fg-dim border-border hover:border-border-light",
    in:  "bg-accent/15 text-accent border-accent/30",
    ex:  "bg-err/15 text-err border-err/30 line-through",
  };
  const prefix = state === "in" ? "+ " : state === "ex" ? "− " : "";
  const hint = state === "off"
    ? "OF built-in"
    : state === "in"
      ? "included"
      : "excluded";
  return (
    <button
      type="button"
      onClick={onClick}
      title="Click to cycle: off → include → exclude → off"
      className={
        "px-2 py-1 rounded-full border text-[11px] flex items-center gap-1.5 transition-colors " +
        stateStyles[state]
      }
    >
      <span>{prefix}{label}</span>
      <span className="opacity-60 no-underline">· {hint}</span>
    </button>
  );
}
