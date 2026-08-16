"use client";

/**
 * Composer — message-input + send button + emoji controls + PPV + vault.
 *
 * Send routes through the chat's owning account_id, not the global scope
 * — this is the whole point of Unified Inbox. Parent passes accountId
 * (== chat.__accountId) so this works whether scope is "all" or a single
 * model.
 *
 * Enter sends; Shift+Enter inserts a newline. Disabled while inflight to
 * prevent double-submits; the spinner is purely visual since the
 * optimistic bubble appears instantly anyway.
 *
 * PPV controls: a "$" toggle reveals a price input + lock-text checkbox.
 * On send we pass `price` and `lockedText` to the parent.
 *
 * Vault: 📎 button opens VaultPicker over the chat surface. Selected
 * media show as preview chips above the textarea; click X on a chip to
 * drop it. Their ids ride along on send as `mediaFiles`.
 */

import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from "react";

import { Button } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";
import { proxyImage, type VaultMedia } from "@/lib/relay";
import { fmtDuration } from "@/lib/format";
import { isOverlongForMessage, MESSAGE_VIDEO_LIMIT_S } from "@/lib/sendFailure";
import { useFanVaultHistory } from "@/hooks/useFanVaultHistory";
import { useReorder } from "@/hooks/useReorder";
import { useSavedReplies } from "@/hooks/useSavedReplies";

import dynamic from "next/dynamic";

import { EmojiPickerButton, EmojiQuickRow, insertAtCursor } from "./EmojiBar";
import { LinesPicker } from "./LinesPicker";
import { useFanLines, type FanLine } from "@/hooks/useFanLines";
import { GifPickerButton, GifPickerStrip, type PickedGif } from "./GifPicker";
import {
  TemplatePicker,
  templateMediaToVault,
  fromLocal as savedReplyToPicked,
  type PickedTemplate,
} from "./TemplatePicker";
import { TagCreatorsPicker, TaggedCreatorChips, type TaggedCreatorChoice } from "./TagCreatorsPicker";

// VaultPicker is heavy (hover-scrub state, fan-vault-history, wall-media,
// per-fan MRU localStorage). The composer mounts on every chat surface,
// but the picker only renders when the chatter clicks 📎. Dynamic +
// render-gate keeps the picker chunk off the inbox boot bundle.
const VaultPicker = dynamic(
  () => import("./VaultPicker").then((m) => m.VaultPicker),
  { ssr: false },
);

export interface SendArgs {
  text: string;
  price?: number;
  lockedText?: boolean;
  /** Vault items the sender attached. The hook extracts ids for the
   *  wire payload AND uses the file URLs to seed the optimistic bubble's
   *  media — without that, attached PPV messages render text-only. */
  attached?: VaultMedia[];
  /** Vault media ids that should be sent UNLOCKED (free previews) when
   *  `price > 0`. Order doesn't matter to OF — the locked/unlocked split
   *  is per-media-id, not by position. Omit / `[]` ⇒ everything locked. */
  previews?: number[];
  /** ISO 8601 send-at timestamp. When set, the relay queues via the
   *  /messages/scheduled endpoint instead of sending immediately. */
  scheduledAt?: string;
  /** OF user ids of creators to @-tag (sent as `userTags`). Picker
   *  guarantees these are valid taggable creators — OF 400s otherwise. */
  taggedUsers?: number[];
  /** Giphy id (e.g. "0ndspgUFbm9iQtyX2Q"). OF accepts a single GIF per
   *  send — when the user picks multiple, the parent splits the call. */
  giphyId?: string;
}

/** Imperative API exposed via `composerApiRef`. The parent can call
 *  these to drive the composer from outside (e.g. FanDrawer's
 *  "Send to chat" buttons on unsold PPVs). */
export interface ComposerApi {
  /** Replace text + attachments + price wholesale, identical to picking
   *  a saved template. Use this to populate the composer from arbitrary
   *  sources (template picker, prior message, unsold PPV, etc). */
  applyTemplate(t: PickedTemplate): void;
}

export interface ComposerProps {
  accountId: string | null;
  /** When the composer is bound to a single fan (chat surface), passing
   *  fanId here unlocks the vault picker's per-fan badges + filter. Mass-
   *  send composers leave this null. */
  fanId?: number | null;
  onSend: (args: SendArgs) => void | Promise<void>;
  disabled?: boolean;
  inflight?: number;
  placeholder?: string;
  /** When OF reports the chat as un-sendable (e.g., unsubscribed fan),
   *  pass `canSend=false` + `cannotSendReason` so the composer disables
   *  the controls and surfaces the reason instead of letting the user
   *  burn time on a guaranteed-fail send. */
  canSend?: boolean;
  cannotSendReason?: string | null;
  /** Quoted-reply context. When set, we render a preview chip above the
   *  textarea — the parent is responsible for prepending the quote text
   *  to the actual send body so the wire shape matches OF's expectations. */
  quoted?: { messageId: number; preview: string; authorName: string } | null;
  onClearQuoted?: () => void;
  /** Optional outbound ref for parents that need to drive the composer
   *  imperatively. The composer attaches a `ComposerApi` to `.current`
   *  for the lifetime of the mount; null on unmount. */
  composerApiRef?: React.MutableRefObject<ComposerApi | null>;
}

export function Composer({
  accountId, fanId = null, onSend, disabled, inflight = 0, placeholder,
  canSend: chatCanSend = true, cannotSendReason,
  quoted, onClearQuoted,
  composerApiRef,
}: ComposerProps) {
  const [text, setText] = useState("");
  const [priceOpen, setPriceOpen] = useState(false);
  const [price, setPrice] = useState<string>("");
  // Default OFF per DA ruling on asks #6 + #17: lock-text is opt-in. The
  // one-time migration toast below explains the change to returning users.
  const [lockText, setLockText] = useState(false);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [attached, setAttached] = useState<VaultMedia[]>([]);
  // How many leading tiles are FREE previews; rest are PPV-locked. Default
  // 1 once the user attaches any media in PPV mode (matches OF desktop's
  // "first tile is the unlocked teaser" convention) — but only if the user
  // hasn't already adjusted it. The touched-ref below preserves explicit
  // user choices (including "0 previews — fully locked").
  const [previewCount, setPreviewCount] = useState<number>(0);
  const previewCountTouchedRef = useRef(false);
  const bumpPreviewCount = (updater: (c: number) => number) => {
    previewCountTouchedRef.current = true;
    setPreviewCount(updater);
  };
  const [scheduleOpen, setScheduleOpen] = useState(false);
  const [scheduleAt, setScheduleAt] = useState<string>("");
  const [taggedCreators, setTaggedCreators] = useState<TaggedCreatorChoice[]>([]);
  // Picked GIFs. OF accepts ONE GIF per send (top-level `giphyId`, sibling
  // of `mediaFiles`). When the user picks multiple, the submit handler
  // fans them out as separate sends — verified live 2026-05-23.
  const [pickedGifs, setPickedGifs] = useState<PickedGif[]>([]);
  const [gifPickerOpen, setGifPickerOpen] = useState(false);
  const [quickMenuOpen, setQuickMenuOpen] = useState(false);
  const quickMenuRef = useRef<HTMLDivElement | null>(null);
  /** Transient confirmation after a quick-send / scheduler fires. Lets the
   *  user know the message is queued locally — the textarea clears, so
   *  without this the action looks like nothing happened. */
  const [sendToast, setSendToast] = useState<string | null>(null);
  const toastTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  // Per-fan vault history — drives the green "BOUGHT" badge so the chatter
  // can see at a glance which attached tiles the fan already paid for (and
  // can avoid charging again). Only meaningful for single-fan composers
  // (fanId set); mass-send variants render gray/red without bought-state.
  const fanVaultQ = useFanVaultHistory(accountId, fanId ?? null, fanId != null);

  // Script-cursor state — remembers the last sent script step per chat so
  // the composer can suggest the next step in the same script. The cursor
  // survives reloads (localStorage) and is sticky: typing a one-off reply
  // between script steps does NOT clear it, so the user can resume the
  // sequence after side-conversations. Cleared on manual dismiss only.
  const [scriptCursor, setScriptCursor] = useScriptCursor(accountId, fanId ?? null);
  // Captures the most-recently-picked template so a successful submit can
  // advance the script cursor on the same render. A ref (not state) keeps
  // the side-effect off the dependency chain.
  const lastPickedRef = useRef<PickedTemplate | null>(null);
  // A "Lines" pick (Q/Tease) inserted into the box; its slot is consumed (deleted
  // from the DB) once the message is actually sent, so it's never offered twice.
  const { consume: consumeLine } = useFanLines(accountId, fanId ?? null, false);
  const armedLineRef = useRef<string | null>(null);
  // We only need the saved-reply list when a cursor exists — gate the
  // fetch so cold chats don't pay a /admin/saved-replies roundtrip just
  // to render the empty composer.
  const replyQ = useSavedReplies(scriptCursor ? accountId : null);
  const nextStepReply = useMemo(() => {
    if (!scriptCursor || !replyQ.data) return null;
    const target = scriptCursor.step + 1;
    return (
      replyQ.data.find(
        (r) =>
          (r.script_id ?? "") === scriptCursor.scriptId &&
          r.script_step === target,
      ) ?? null
    );
  }, [scriptCursor, replyQ.data]);

  const numericPrice = parsePrice(price);
  const hasAttachments = attached.length > 0;
  // Attachments long enough that OF is likely to answer "Something wrong with
  // attached media" and kill the whole send. Advisory: the ceiling is inferred
  // from five live sends, so this warns and never blocks (lib/sendFailure).
  const overlong = useMemo(() => attached.filter(isOverlongForMessage), [attached]);
  const reorder = useReorder(attached, setAttached, (m) => m.id);
  // Clamp on read so removing tiles never leaves previewCount past the
  // end (would render phantom FREE badges on indices beyond the array).
  const effectivePreviewCount = Math.max(0, Math.min(previewCount, attached.length));
  const showPpv = numericPrice != null && numericPrice > 0;
  const showDivider =
    showPpv && effectivePreviewCount > 0 && effectivePreviewCount < attached.length;
  const hasGifs = pickedGifs.length > 0;
  const canSend =
    !disabled &&
    chatCanSend &&
    (text.trim().length > 0 || hasAttachments || hasGifs) &&
    (!priceOpen || numericPrice == null || numericPrice >= 0);
  // Surface OF's block reason at the top of the composer when sending is
  // disallowed. Saves the user from typing a message that will 400.
  const blocked = !chatCanSend;

  function showToast(msg: string) {
    if (toastTimerRef.current) clearTimeout(toastTimerRef.current);
    setSendToast(msg);
    toastTimerRef.current = setTimeout(() => setSendToast(null), 6000);
  }

  // Clear any pending toast/migration-banner timer on unmount so it can't
  // fire setSendToast after the composer is gone (chat switches remount it).
  useEffect(() => () => {
    if (toastTimerRef.current) clearTimeout(toastTimerRef.current);
  }, []);

  async function submit(opts?: { delayMinutes?: number }) {
    const body = text.trim();
    if (!body && !hasAttachments && !hasGifs) return;
    const args: SendArgs = { text: body };
    // Paid path: a typed price > 0 is the sole signal. (The previous
    // "Send as paid" toggle was redundant — if a price is set, the message
    // is PPV; clear the price to send free.)
    if (numericPrice != null && numericPrice > 0) {
      args.price = numericPrice;
      args.lockedText = lockText;
      if (effectivePreviewCount > 0) {
        args.previews = attached
          .slice(0, effectivePreviewCount)
          .filter(canBeFreePreview)
          .map((m) => m.id);
      }
    }
    if (hasAttachments) args.attached = attached;
    if (taggedCreators.length > 0) {
      args.taggedUsers = taggedCreators.map((c) => c.id);
    }
    // OF accepts ONE GIF per send (verified live 2026-05-23). When the
    // user picked multiple, ride the first one on this send and fan out
    // the rest as standalone GIF-only sends below.
    const firstGif = pickedGifs[0];
    const trailingGifs = pickedGifs.slice(1);
    if (firstGif) args.giphyId = firstGif.id;
    // Quick-send delay (1/3/5/7/10/15) takes priority over the explicit
    // scheduler row — they're alternate UIs for the same idea.
    if (opts?.delayMinutes && opts.delayMinutes > 0) {
      args.scheduledAt = new Date(Date.now() + opts.delayMinutes * 60_000).toISOString();
    } else if (scheduleOpen && scheduleAt) {
      // datetime-local has no tz suffix. Treat the value as local-time and
      // let the Date constructor attach the user's tz when serializing.
      const d = new Date(scheduleAt);
      if (!Number.isNaN(d.getTime()) && d.getTime() > Date.now()) {
        args.scheduledAt = d.toISOString();
      }
    }
    // Decide which kind of "queued" toast to show. setSendToast also
    // happens for immediate sends so it's a quiet "sent" confirmation —
    // but only when scheduled, where the textarea clears with no bubble
    // to indicate progress (the scheduled ghost bubble shows it instead).
    // BOTH branches are server-side now: ≤15 min goes on our executor queue,
    // longer on OF's — either way it survives reload + every chatter sees it.
    if (args.scheduledAt) {
      const fireAt = new Date(args.scheduledAt).getTime();
      const minsFromNow = Math.max(1, Math.round((fireAt - Date.now()) / 60_000));
      if (fireAt - Date.now() <= 15 * 60 * 1000) {
        showToast(`⏱ Scheduled · sending in ${minsFromNow} min (saved — survives reload)`);
      } else {
        showToast(`⏱ Queued on OF for ${new Date(args.scheduledAt).toLocaleString()}`);
      }
    }
    setText("");
    setPrice("");
    setPriceOpen(false);
    setAttached([]);
    setPreviewCount(0);
    previewCountTouchedRef.current = false;
    setScheduleOpen(false);
    setScheduleAt("");
    setTaggedCreators([]);
    setPickedGifs([]);
    setGifPickerOpen(false);
    setQuickMenuOpen(false);
    await onSend(args);
    // A "Lines" pick that actually went out (non-empty text) is consumed so the
    // same question/tease is never offered again for this fan.
    const armedSlot = armedLineRef.current;
    armedLineRef.current = null;
    if (armedSlot && (args.text || "").trim()) {
      void consumeLine(armedSlot);
    }
    // Fan out additional GIFs as standalone sends. Each carries only its
    // own giphyId — no text, no attachments — to mirror OF web behavior.
    for (const g of trailingGifs) {
      await onSend({ text: "", giphyId: g.id });
    }
    // Advance the script cursor if the just-sent message rode a template
    // with script_id + script_step set. Picked template is captured in
    // onPickTemplate above; cleared here so an unrelated next send won't
    // re-advance from this same template.
    const picked = lastPickedRef.current;
    lastPickedRef.current = null;
    if (picked?.scriptId && typeof picked.scriptStep === "number") {
      setScriptCursor({ scriptId: picked.scriptId, step: picked.scriptStep });
    }
    setTimeout(() => textareaRef.current?.focus(), 0);
  }

  // Close the quick-send dropdown on outside click — same pattern as the
  // per-chat scheduled popover. Without this it sits there after picking.
  useEffect(() => {
    if (!quickMenuOpen) return;
    const onDown = (e: MouseEvent) => {
      if (quickMenuRef.current && !quickMenuRef.current.contains(e.target as Node)) {
        setQuickMenuOpen(false);
      }
    };
    window.addEventListener("pointerdown", onDown);
    return () => window.removeEventListener("pointerdown", onDown);
  }, [quickMenuOpen]);

  // Autofocus the textarea on mount. The Composer remounts whenever the
  // user picks a different chat (ChatSurface passes a `key` upstream),
  // so this gives them a typing cursor the same frame the chat opens —
  // no extra click needed.
  useEffect(() => {
    textareaRef.current?.focus();
  }, []);

  // No auto-seed: 0 previews is a legitimate choice on PPV (sends
  // everything locked). The ± stepper lets the user opt into a leading
  // free teaser when they want one.

  // One-time migration banner: the lockedText default flipped from true →
  // false (asks #6 + #17). Returning users who had muscle memory for the
  // old "lock text auto-on" need a heads-up. localStorage flag fires once
  // per browser, not per mount.
  useEffect(() => {
    if (typeof window === "undefined") return;
    const key = "chatterly:lockedText-default-changed-seen";
    if (localStorage.getItem(key)) return;
    if (toastTimerRef.current) clearTimeout(toastTimerRef.current);
    setSendToast("Text is no longer locked by default in paid messages. Tap ‘lock text’ to lock.");
    toastTimerRef.current = setTimeout(() => setSendToast(null), 4000);
    localStorage.setItem(key, "1");
  }, []);

  function onKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      if (canSend) submit();
    }
  }

  function onEmoji(em: string) {
    const ta = textareaRef.current;
    if (!ta) {
      setText((t) => t + em);
      return;
    }
    insertAtCursor(ta, text, em, setText);
  }

  function onPicked(picked: VaultMedia[]) {
    // Replace selection wholesale — the picker pre-selects from
    // `attached` so any removals/adds done inside it stick on confirm.
    setAttached(picked);
    // Auto-price: the operator sets a per-media price in /vault, and a bundle is
    // worth the HIGHEST of what's in it — never undercharge for the best clip in
    // the set. Media with no price contribute nothing, so attaching unpriced
    // media leaves whatever price you already typed alone.
    const top = picked.reduce((max, m) => {
      const c = (m as { _ai?: { suggested_price_cents?: number | null } })._ai
        ?.suggested_price_cents;
      return typeof c === "number" && c > max ? c : max;
    }, 0);
    if (top > 0) {
      setPrice((top / 100).toFixed(2));
      setPriceOpen(true);
    }
  }

  function removeAttachment(id: number) {
    setAttached((prev) => prev.filter((m) => m.id !== id));
  }

  function onPickLine(l: FanLine) {
    // Insert the line at the cursor (chatter can tweak), and arm its slot so a
    // subsequent successful send consumes it from the DB.
    const ta = textareaRef.current;
    if (ta) insertAtCursor(ta, text, l.text, setText);
    else setText((t) => (t ? `${t} ${l.text}` : l.text));
    armedLineRef.current = l.slot;
  }

  function onPickTemplate(t: PickedTemplate) {
    // Capture the picked template so a subsequent successful send can
    // advance the script cursor. Cleared by submit() so a non-template
    // send won't accidentally re-advance.
    lastPickedRef.current = t;
    // Template = snapshot. Replace text + attachments + preview-count
    // wholesale so the FREE-first ordering baked in at save time
    // survives. Merging would push the template's free tiles behind
    // whatever was already attached and break the count-based preview
    // model (leading N tiles = free).
    setText(t.text);
    setAttached(templateMediaToVault(t));
    // Reset price wholesale — a free template (price 0) must clear any
    // price the user already typed, or a "free" template would silently
    // go out as a stale PPV (numericPrice > 0 is the sole paid signal).
    setPrice(t.price > 0 ? String(t.price) : "");
    setLockText(t.price > 0 ? t.lockedText : false);
    setPriceOpen(t.price > 0);
    // Always lock previewCount to the template's count — 0 is a valid
    // "everything PPV-locked" state that must overwrite any prior value.
    previewCountTouchedRef.current = true;
    setPreviewCount(t.previews.length);
    if (t.taggedUsers.length > 0) {
      // The picker resolves names lazily; we only have ids in the
      // template. Seed placeholder chips so the user can see the tag
      // count immediately — opening the @ popover will replace them
      // with the live data on next render.
      setTaggedCreators((prev) => {
        const have = new Set(prev.map((c) => c.id));
        const additions: TaggedCreatorChoice[] = t.taggedUsers
          .filter((id) => !have.has(id))
          .map((id) => ({ id, name: `user${id}`, username: "", avatar: null }));
        return [...prev, ...additions];
      });
    }
    // GIF state is part of the snapshot too: seed the template's GIF when
    // it carries one, otherwise CLEAR any previously-picked GIF so a stale
    // pick doesn't ride along on a non-GIF template's send.
    if (t.gifId) {
      const url = t.gifUrl ?? "";
      setPickedGifs([{ id: t.gifId, thumbUrl: url, fullUrl: url, title: t.title }]);
    } else {
      setPickedGifs([]);
    }
    setTimeout(() => textareaRef.current?.focus(), 0);
  }

  // Publish the imperative API to the parent's ref while we're mounted.
  // Stored via a stable inner ref so `onPickTemplate`'s identity churn
  // (it captures every render's closures) doesn't keep re-firing the
  // outer effect; we just update the function inside the box each render.
  const composerApiBoxRef = useRef<ComposerApi>({
    applyTemplate: () => { /* set on first render */ },
  });
  composerApiBoxRef.current.applyTemplate = onPickTemplate;
  useEffect(() => {
    if (!composerApiRef) return;
    composerApiRef.current = composerApiBoxRef.current;
    return () => { composerApiRef.current = null; };
  }, [composerApiRef]);

  return (
    <>
      <div
        data-composer-area
        // shrink-0: never let the flex parent compress the composer when
        //   the keyboard or a tall preview tray pushes the page.
        // pb safe-area: keeps the row above iOS Safari's home-indicator bar.
        className="shrink-0 border-t border-border px-3 pt-3 pb-[max(0.75rem,env(safe-area-inset-bottom))] space-y-2 bg-panel"
      >
        {blocked && (
          <div className="text-[11px] px-2.5 py-1.5 rounded-md border bg-err/10 border-err/30 text-err">
            ⚠ Sending disabled
            {cannotSendReason ? ` — ${cannotSendReason}` : " — OF blocked this chat."}
          </div>
        )}
        {quoted && (
          <div className="flex items-start gap-2 text-[11px] px-2.5 py-1.5 rounded-md border border-accent/30 bg-accent/10">
            <div className="border-l-2 border-accent/60 pl-2 flex-1 min-w-0">
              <div className="text-accent font-medium">Replying to {quoted.authorName}</div>
              <div className="text-fg-dim truncate">{quoted.preview}</div>
            </div>
            <button
              type="button"
              onClick={() => onClearQuoted?.()}
              className="text-fg-dim hover:text-fg"
              title="Cancel reply"
            >
              ✕
            </button>
          </div>
        )}
        <div className="flex items-center justify-between">
          <EmojiQuickRow onInsert={onEmoji} disabled={disabled || blocked} />
          {inflight > 0 ? (
            <span className="text-[10px] text-fg-dim">{inflight} sending…</span>
          ) : (
            <span className="hidden md:inline text-[10px] text-fg-dim">
              Enter to send · Shift+Enter for newline
            </span>
          )}
        </div>

        {scheduleOpen && (
          <div className="flex items-center gap-2 bg-bg/60 border border-border rounded-lg px-2.5 py-1.5 flex-wrap">
            <span className="text-xs text-fg-dim">Send at</span>
            <input
              type="datetime-local"
              value={scheduleAt}
              onChange={(e) => setScheduleAt(e.target.value)}
              min={localNowForInput()}
              className="bg-transparent border-0 px-1 py-0.5 text-base md:text-sm focus:outline-none"
            />
            <div className="flex items-center gap-1">
              {[5, 10, 15, 30, 60].map((mins) => (
                <button
                  key={mins}
                  type="button"
                  onClick={() => setScheduleAt(localFutureForInput(mins))}
                  className="text-[10px] px-1.5 py-0.5 rounded border border-border text-fg-dim hover:text-fg hover:bg-bg-elev-1"
                  title={`Send in ${mins} minutes`}
                >
                  +{mins}m
                </button>
              ))}
              {scheduleAt && (
                <button
                  type="button"
                  onClick={() => setScheduleAt("")}
                  className="text-[10px] px-1.5 py-0.5 rounded text-fg-dim hover:text-fg"
                  title="Clear"
                >
                  ✕
                </button>
              )}
            </div>
            <span className="ml-auto text-[10px] text-fg-dim">
              {scheduleAt
                ? new Date(scheduleAt) > new Date()
                  ? `${describeSchedule(scheduleAt)}`
                  : "Pick a future time"
                : "Pick a date/time or use a preset"}
            </span>
          </div>
        )}

        {priceOpen && showPpv && (
          <div className="flex items-center gap-2 bg-bg/60 border border-border rounded-lg px-2.5 py-1.5">
            <span className="text-xs text-fg-dim">PPV</span>
            <div className="flex items-center gap-1 text-sm">
              <span className="text-fg-dim">$</span>
              <input
                type="text"
                inputMode="decimal"
                value={price}
                onChange={(e) => setPrice(sanitizePriceInput(e.target.value))}
                placeholder="0.00"
                className="w-20 bg-transparent border-0 px-1 py-0.5 text-base md:text-sm focus:outline-none placeholder:text-muted"
              />
            </div>
            <label className="text-xs text-fg-dim flex items-center gap-1.5 cursor-pointer select-none">
              <input
                type="checkbox"
                checked={lockText}
                onChange={(e) => setLockText(e.target.checked)}
                className="accent-accent"
              />
              lock text
            </label>
            <span className="ml-auto text-[10px] text-fg-dim">
              {numericPrice != null && numericPrice > 0
                ? `Fan pays $${numericPrice.toFixed(2)} to read`
                : "Set a price > 0 to send as paid"}
            </span>
          </div>
        )}

        {sendToast && (
          <div className="text-[11px] px-2.5 py-1.5 rounded-md border bg-accent/10 border-accent/30 text-accent flex items-center gap-2">
            <span className="flex-1">{sendToast}</span>
            <button
              type="button"
              onClick={() => setSendToast(null)}
              className="text-fg-dim hover:text-fg"
              title="Dismiss"
            >
              ✕
            </button>
          </div>
        )}

        <TaggedCreatorChips selected={taggedCreators} onChange={setTaggedCreators} />

        {hasAttachments && (
          <div className="flex items-center gap-1.5 flex-wrap bg-bg/60 border border-border rounded-lg px-2 py-2">
            {/* Preview-count stepper — controls how many leading tiles are
             *  FREE teasers. Visible only in PPV mode (free messages have
             *  no preview/paid split). Reposition via drag, or use ± here. */}
            {showPpv && (
              <div
                className="flex items-center gap-1 text-[10px] text-fg-dim mr-1"
                title="How many tiles are free previews. Drag a tile across the divider to flip its side."
              >
                <span className="uppercase tracking-wide">previews</span>
                <button
                  type="button"
                  onClick={() => bumpPreviewCount((c) => Math.max(0, c - 1))}
                  disabled={effectivePreviewCount === 0}
                  className="w-8 h-8 md:w-4 md:h-4 grid place-items-center rounded border border-border hover:bg-bg-elev-1 disabled:opacity-30"
                  aria-label="Fewer free previews"
                >−</button>
                <span className="tabular-nums text-fg">
                  {effectivePreviewCount}/{attached.length}
                </span>
                <button
                  type="button"
                  onClick={() => bumpPreviewCount((c) => Math.min(attached.length, c + 1))}
                  disabled={effectivePreviewCount >= attached.length}
                  className="w-8 h-8 md:w-4 md:h-4 grid place-items-center rounded border border-border hover:bg-bg-elev-1 disabled:opacity-30"
                  aria-label="More free previews"
                >+</button>
              </div>
            )}
            {attached.map((m, idx) => {
              const rawThumb =
                m.files?.thumb?.url ||
                m.files?.squarePreview?.url ||
                m.files?.preview?.url ||
                null;
              const thumb = proxyImage(rawThumb, accountId);
              // A tile only renders FREE if it's within the preview range
              // AND would actually survive the `previews` filter on send. A
              // still-uploading `_claim` tile is sent as media but cannot be
              // referenced by id in `previews`, so OF locks it — showing FREE
              // there would lie about what the fan sees. Render it as PPV.
              const isFreeTile = idx < effectivePreviewCount && canBeFreePreview(m);
              const isDragging = reorder.draggingIndex === idx;
              const isDragOver = reorder.dragOverIndex === idx && !isDragging;
              const priceLabel = numericPrice != null && numericPrice > 0
                ? `$${numericPrice % 1 === 0 ? numericPrice : numericPrice.toFixed(2)}`
                : "";
              // Three-state badge: BOUGHT (green) wins over the PPV split —
              // this fan already paid for this vault item, so the chatter
              // should know before sending it again. Falls through to the
              // free/paid colors below when no purchase record exists.
              const fanEntry = fanVaultQ.data?.by_media[String(m.id)];
              const wasBought = fanEntry?.was_purchased === true;
              // Long videos come back as "Something wrong with attached media"
              // — a 400 that names the id but costs the whole send. Flag the
              // chip rather than refusing it: the exact ceiling is inferred,
              // not documented (see lib/sendFailure).
              const tooLong = isOverlongForMessage(m);
              return (
                <Fragment key={m.id}>
                  {showDivider && idx === effectivePreviewCount && (
                    <div
                      className="flex flex-col items-center justify-center gap-0.5 h-16 px-1 text-[8px] font-semibold uppercase tracking-wide select-none border-l-2 border-r-2 border-dashed border-fg-dim/60"
                      title="Items left of this line are free previews; items right are PPV-locked"
                      aria-hidden
                    >
                      <span className="text-fg-dim leading-none">free ↑</span>
                      <span className="text-muted leading-none">/</span>
                      <span className="text-err leading-none">paid ↓</span>
                    </div>
                  )}
                  <div
                    draggable={!disabled}
                    onDragStart={(e) => reorder.onDragStart(e, idx)}
                    onDragOver={(e) => reorder.onDragOver(e, idx)}
                    onDrop={(e) => reorder.onDrop(e, idx)}
                    onDragEnd={reorder.onDragEnd}
                    tabIndex={0}
                    onKeyDown={(e) => {
                      if (e.key === "ArrowLeft") {
                        e.preventDefault();
                        reorder.onKeyboardMove(idx, -1);
                      } else if (e.key === "ArrowRight") {
                        e.preventDefault();
                        reorder.onKeyboardMove(idx, 1);
                      }
                    }}
                    className={cn(
                      "relative w-16 h-16 rounded-md overflow-hidden border bg-bg-elev-1 group select-none",
                      "motion-safe:transition-transform motion-safe:duration-150",
                      "cursor-grab active:cursor-grabbing",
                      "focus:outline-none focus:ring-2 focus:ring-accent",
                      isDragging ? "opacity-40 border-accent" : "border-border",
                      isDragOver && "ring-2 ring-accent",
                    )}
                  >
                    {thumb ? (
                      <img src={thumb} alt="" loading="lazy" decoding="async" draggable={false} className="w-full h-full object-cover pointer-events-none" />
                    ) : (
                      <div className="w-full h-full grid place-items-center text-[10px] text-fg-dim">
                        {m.type}
                      </div>
                    )}
                    {(showPpv || wasBought) && (
                      <div
                        className={cn(
                          "absolute top-0.5 left-0.5 px-1 rounded text-[9px] font-bold leading-tight text-white shadow",
                          wasBought
                            ? "bg-ok"
                            : isFreeTile ? "bg-fg-dim" : "bg-err",
                        )}
                        title={
                          wasBought
                            ? "BOUGHT — this fan has already purchased this item"
                            : isFreeTile
                              ? "Free preview — fans see this before paying"
                              : "PPV-locked — fans see this after paying"
                        }
                      >
                        {wasBought ? "BOUGHT" : isFreeTile ? "FREE" : priceLabel || "PPV"}
                      </div>
                    )}
                    <button
                      type="button"
                      onClick={() => removeAttachment(m.id)}
                      className="absolute top-0.5 right-0.5 w-6 h-6 md:w-4 md:h-4 rounded-full bg-black/70 text-white grid place-items-center text-[10px] opacity-80 hover:opacity-100"
                      aria-label="Remove attachment"
                    >
                      ✕
                    </button>
                    {tooLong && (
                      <div
                        className="absolute left-0.5 bottom-5 px-1 rounded bg-warn text-black text-[9px] font-bold leading-tight shadow"
                        title={`${fmtDuration(m.duration)} — over the ${fmtDuration(MESSAGE_VIDEO_LIMIT_S)} an attachment usually has to stay under`}
                      >
                        ⚠ {fmtDuration(m.duration)}
                      </div>
                    )}
                    {/* Per-row inline price. Every chip writes to the single
                     *  composer-level `price` state, so they all mirror each
                     *  other — typing in row 2 updates rows 1 and 3 too. */}
                    <div className="absolute inset-x-0 bottom-0 flex items-center bg-black/50 px-1 gap-0.5">
                      <span className="text-white/85 text-[12px] leading-none">$</span>
                      <input
                        type="text"
                        inputMode="decimal"
                        value={price}
                        onChange={(e) => {
                          const cleaned = sanitizePriceInput(e.target.value);
                          setPrice(cleaned);
                          if (cleaned && !priceOpen) setPriceOpen(true);
                        }}
                        placeholder="free"
                        aria-label="PPV price"
                        className="flex-1 min-w-0 bg-transparent border-0 text-white text-base md:text-[12px] placeholder:text-white/60 focus:outline-none py-0.5"
                      />
                    </div>
                  </div>
                </Fragment>
              );
            })}
            <button
              type="button"
              onClick={() => setPickerOpen(true)}
              disabled={disabled || !accountId}
              className="w-16 h-16 rounded-md border border-dashed border-border bg-transparent hover:bg-bg-elev-1 text-fg-dim hover:text-fg grid place-items-center text-lg"
              title="Add more"
            >
              +
            </button>
          </div>
        )}

        {overlong.length > 0 && (
          <div
            className="flex items-start gap-1.5 rounded-md border border-warn/40 bg-warn/10 px-2 py-1 text-[11px] leading-snug text-warn"
            role="status"
          >
            <span aria-hidden>⚠</span>
            <span>
              {`${
                overlong.length === 1
                  ? `That ${fmtDuration(overlong[0].duration)} video is`
                  : `${overlong.length} attachments are`
              } longer than ${fmtDuration(MESSAGE_VIDEO_LIMIT_S)}. OF often refuses those`
                + ` outright, which fails the whole send — post it or trim it if this bounces.`}
            </span>
          </div>
        )}

        {gifPickerOpen && (
          <GifPickerStrip
            accountId={accountId}
            onClose={() => setGifPickerOpen(false)}
            onPick={(g) => {
              setPickedGifs((prev) => (
                prev.some((p) => p.id === g.id) ? prev : [...prev, g]
              ));
              setGifPickerOpen(false);
            }}
          />
        )}

        {pickedGifs.length > 0 && (
          <div className="flex items-center gap-1.5 flex-wrap bg-bg/60 border border-border rounded-lg px-2 py-2">
            <span className="text-[10px] uppercase tracking-wide text-fg-dim mr-1">gifs</span>
            {pickedGifs.map((g) => (
              <div key={g.id} className="relative w-16 h-16 rounded-md overflow-hidden border border-border bg-bg-elev-1">
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img src={g.thumbUrl} alt={g.title || "GIF"} className="w-full h-full object-cover" />
                <button
                  type="button"
                  onClick={() => setPickedGifs((prev) => prev.filter((p) => p.id !== g.id))}
                  className="absolute top-0.5 right-0.5 w-6 h-6 md:w-4 md:h-4 rounded-full bg-black/70 text-white grid place-items-center text-[10px] opacity-80 hover:opacity-100"
                  aria-label="Remove GIF"
                >
                  ✕
                </button>
              </div>
            ))}
            <span className="ml-auto text-[10px] text-fg-dim">
              {pickedGifs.length > 1
                ? `${pickedGifs.length} GIFs — sent as separate messages`
                : "1 GIF attached"}
            </span>
          </div>
        )}

        {nextStepReply && (
          <div
            role="button"
            tabIndex={0}
            onClick={() => onPickTemplate(savedReplyToPicked(nextStepReply))}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                onPickTemplate(savedReplyToPicked(nextStepReply));
              }
            }}
            title="Tap to load this step into the composer"
            className="flex items-center gap-2 text-[11px] px-2.5 py-1.5 rounded-md border border-accent/40 bg-accent/10 text-accent cursor-pointer hover:bg-accent/15 focus:outline-none focus:ring-2 focus:ring-accent/50 select-none"
          >
            <span className="font-medium shrink-0">
              ▶ Next in “{nextStepReply.script_id}” · step {nextStepReply.script_step}
            </span>
            <span className="flex-1 min-w-0 text-fg-dim truncate">
              {nextStepReply.title ? <strong className="text-fg mr-1">{nextStepReply.title}:</strong> : null}
              {nextStepReply.text}
            </span>
            <button
              type="button"
              onClick={(e) => {
                // Stop the row's click handler from also firing the pick —
                // ✕ is an "end script" action, not a "pick + dismiss" combo.
                e.stopPropagation();
                setScriptCursor(null);
              }}
              className="text-fg-dim hover:text-fg shrink-0"
              title="End script — stop suggesting next steps"
            >
              ✕
            </button>
          </div>
        )}

        {/* Toolbar — every icon button lives in this single row above the
         *  textarea so the composer reads top-to-bottom: alerts, controls,
         *  type-and-send. User can drag the order later; for now this is
         *  the same set of buttons that used to be stacked vertically. */}
        <div className="flex items-center gap-1.5 flex-wrap">
          <button
            type="button"
            onClick={() => setPickerOpen(true)}
            disabled={disabled || !accountId}
            title="Pick from vault"
            className={cn(
              "h-8 px-2.5 inline-flex items-center gap-1.5 rounded-md border text-sm",
              hasAttachments
                ? "bg-accent/15 text-accent border-accent/40"
                : "bg-transparent text-fg-dim border-border hover:bg-bg-elev-1",
              (disabled || !accountId) && "opacity-40 cursor-not-allowed",
            )}
          >
            <span>📎</span>
            <span>Vault</span>
          </button>
          {/* Inline PPV $ field — single source of truth for price.
           *  Replaces the old "Send as paid" toggle: with a single price
           *  input there's no separate intent flag, since price > 0 already
           *  means paid and empty/0 means free. */}
          <label
            className={cn(
              "h-8 px-2 flex items-center gap-1 rounded-md border text-[11px] select-none",
              showPpv
                ? "bg-accent/10 text-accent border-accent/30"
                : "bg-transparent text-fg-dim border-border",
              disabled && "opacity-40 cursor-not-allowed",
            )}
            title="PPV price. Type a number to send paid; clear to send free."
          >
            <span className="opacity-80">$</span>
            <input
              type="text"
              inputMode="decimal"
              value={price}
              onChange={(e) => {
                const cleaned = sanitizePriceInput(e.target.value);
                setPrice(cleaned);
                if (cleaned && !priceOpen) setPriceOpen(true);
              }}
              disabled={disabled}
              placeholder="free"
              aria-label="PPV price"
              className="w-16 bg-transparent border-0 px-0 py-0 text-base md:text-[11px] focus:outline-none placeholder:text-muted"
            />
          </label>
          <button
            type="button"
            onClick={() => setScheduleOpen((v) => !v)}
            disabled={disabled}
            title="Schedule send for later"
            className={cn(
              "w-8 h-8 grid place-items-center rounded-md border text-sm",
              scheduleOpen
                ? "bg-accent/15 text-accent border-accent/40"
                : "bg-transparent text-fg-dim border-border hover:bg-bg-elev-1",
              disabled && "opacity-40 cursor-not-allowed",
            )}
          >
            🕐
          </button>
          <EmojiPickerButton onInsert={onEmoji} disabled={disabled} />
          {/* Desk-only toolbar controls. `md:contents` keeps each control a
           *  direct flex child of the toolbar row at >=768px, so the desktop
           *  layout (order + gap-1.5) is untouched. One wrapper per control —
           *  they are not contiguous. */}
          <div className="hidden md:contents">
            <GifPickerButton
              open={gifPickerOpen}
              onToggle={() => setGifPickerOpen((v) => !v)}
              disabled={disabled}
            />
          </div>
          <TemplatePicker accountId={accountId} onPick={onPickTemplate} />
          <LinesPicker accountId={accountId} fanId={fanId} onPick={onPickLine} />
          <div className="hidden md:contents">
            <TagCreatorsPicker
              accountId={accountId}
              selected={taggedCreators}
              onChange={setTaggedCreators}
              disabled={disabled}
            />
          </div>
        </div>

        <div className="flex items-end gap-2">
          <textarea
            ref={textareaRef}
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={onKeyDown}
            rows={2}
            disabled={disabled}
            placeholder={placeholder ?? "Type a message…"}
            // text-base on mobile prevents iOS Safari from auto-zooming
            // when a sub-16px input is focused (defensive — viewport meta
            // also has `userScalable: false`, but the font-size rule is
            // the canonical fix and survives any future viewport-meta
            // experiment).
            className="flex-1 bg-bg border border-border rounded-lg px-3 py-2 text-base md:text-sm placeholder:text-muted focus:outline-none focus:border-accent resize-y min-h-[44px] max-h-48"
          />
          <div className="flex flex-col gap-1.5">
            <div className="relative flex items-stretch" ref={quickMenuRef}>
              <Button
                size="sm"
                onClick={() => submit()}
                disabled={!canSend}
                className="md:rounded-r-none"
              >
                {scheduleOpen && scheduleAt && new Date(scheduleAt) > new Date()
                  ? "Schedule"
                  : "Send"}
              </Button>
              <button
                type="button"
                onClick={() => setQuickMenuOpen((v) => !v)}
                disabled={!canSend}
                title="Send in N minutes"
                className={cn(
                  "hidden md:block px-1.5 rounded-r-md border-l border-accent/30 bg-accent text-bg text-xs",
                  "hover:opacity-90",
                  !canSend && "opacity-40 cursor-not-allowed",
                )}
              >
                ▾
              </button>
              {quickMenuOpen && (
                <div className="absolute right-0 bottom-full mb-1 w-44 bg-panel border border-border rounded-md shadow-lg z-20 overflow-hidden">
                  <div className="px-2 py-1.5 text-[10px] text-fg-dim border-b border-border">
                    Schedule send
                  </div>
                  {[1, 3, 5, 7, 10, 15].map((m) => (
                    <button
                      key={m}
                      type="button"
                      onClick={() => submit({ delayMinutes: m })}
                      disabled={!canSend}
                      className="w-full text-left text-xs px-2.5 py-1.5 hover:bg-bg-elev-1 flex items-center justify-between"
                    >
                      <span>+{m} min</span>
                      <span className="text-[10px] text-fg-dim">
                        {new Date(Date.now() + m * 60_000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}
                      </span>
                    </button>
                  ))}
                </div>
              )}
            </div>
          </div>
        </div>
      </div>

      {/* Render-gated dynamic import: the picker chunk only fetches when
       *  the user actually opens it. Composer mounts on every chat
       *  surface so this matters for cold-inbox latency. */}
      {pickerOpen && (
        <VaultPicker
          open={pickerOpen}
          onClose={() => setPickerOpen(false)}
          accountId={accountId}
          fanId={fanId}
          initialSelectedIds={attached.map((m) => m.id)}
          onConfirm={onPicked}
        />
      )}
    </>
  );
}

/** datetime-local needs `YYYY-MM-DDTHH:MM` in local time (no Z). Build
 *  the current local-time as that shape so we can use it as the `min`
 *  attr — keeps the picker from offering past times. */
function localNowForInput(): string {
  return toLocalInput(new Date());
}

/** Same shape, offset N minutes into the future. Used by the quick-preset
 *  buttons (+5m, +10m, …) so picking a preset feels like a normal input
 *  fill — the input itself shows the resolved time. */
function localFutureForInput(minutes: number): string {
  return toLocalInput(new Date(Date.now() + minutes * 60_000));
}

function toLocalInput(d: Date): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** Describe the schedule in plain English. Both ranges are server-side now and
 *  survive reload; short delays (≤ 15 min) ride our executor queue, longer ones
 *  OF's native queue. */
function describeSchedule(value: string): string {
  const d = new Date(value);
  const deltaMs = d.getTime() - Date.now();
  if (deltaMs <= 15 * 60 * 1000) {
    const mins = Math.max(1, Math.round(deltaMs / 60_000));
    return `Sending in ${mins} min (scheduled)`;
  }
  return `Queued for ${d.toLocaleString()}`;
}


/** Whether an attached tile can ride in the numeric `previews` array (the
 *  free-teaser split). A freshly-uploaded item still transcoding carries a
 *  `_claim` instead of a real vault id, so it CAN be sent as media but
 *  CANNOT be referenced by id in `previews` — OF would lock it. Keeping the
 *  send-time filter and the FREE-badge render in one predicate stops the UI
 *  from promising a free teaser the wire layer silently drops. */
function canBeFreePreview(m: VaultMedia): boolean {
  return !m._claim && Number.isFinite(m.id) && m.id > 0;
}

/** Accept "5", "5.00", "5,50". Returns null on garbage so submit stays
 *  free of guard-clauses on every read. */
function parsePrice(raw: string): number | null {
  const cleaned = raw.replace(/\s/g, "").replace(",", ".");
  if (!cleaned) return null;
  const n = Number(cleaned);
  if (!Number.isFinite(n)) return null;
  return n;
}

interface ScriptCursor {
  scriptId: string;
  step: number;
}

/** Per-chat cursor for template scripts. Stored in localStorage so the
 *  next-step suggestion survives reloads. Sticky — only cleared when the
 *  user explicitly dismisses the bubble (✕) or when no successor template
 *  exists (handled in render). */
function useScriptCursor(
  accountId: string | null,
  fanId: number | null,
): [ScriptCursor | null, (next: ScriptCursor | null) => void] {
  const storageKey = useMemo(
    () => (accountId && fanId != null ? `chatterly:scriptCursor:${accountId}:${fanId}` : null),
    [accountId, fanId],
  );
  const [cursor, setCursor] = useState<ScriptCursor | null>(null);
  useEffect(() => {
    if (!storageKey || typeof window === "undefined") {
      setCursor(null);
      return;
    }
    try {
      const raw = window.localStorage.getItem(storageKey);
      if (!raw) {
        setCursor(null);
        return;
      }
      const parsed = JSON.parse(raw) as ScriptCursor;
      if (parsed && typeof parsed.scriptId === "string" && typeof parsed.step === "number") {
        setCursor(parsed);
      } else {
        setCursor(null);
      }
    } catch {
      setCursor(null);
    }
  }, [storageKey]);
  const save = useCallback(
    (next: ScriptCursor | null) => {
      setCursor(next);
      if (!storageKey || typeof window === "undefined") return;
      try {
        if (next) window.localStorage.setItem(storageKey, JSON.stringify(next));
        else window.localStorage.removeItem(storageKey);
      } catch {
        /* quota / private mode — UI still works for the current session */
      }
    },
    [storageKey],
  );
  return [cursor, save];
}

/** Strip everything except digits and a single decimal separator from
 *  PPV input. Lets the user type "12.56" but bounces letters / "+5" /
 *  multiple dots so a paid send can never ship a malformed body. Empty
 *  string passes through so backspace clears the field cleanly. */
export function sanitizePriceInput(raw: string): string {
  // Allow commas as a decimal alias — `parsePrice` normalises to dot.
  const cleaned = raw.replace(/[^\d.,]/g, "").replace(/,/g, ".");
  const dot = cleaned.indexOf(".");
  if (dot === -1) return cleaned;
  // Keep only the FIRST decimal point; strip subsequent dots.
  return cleaned.slice(0, dot + 1) + cleaned.slice(dot + 1).replace(/\./g, "");
}
