"use client";

/**
 * TemplatesTab — manage saved replies + the welcome message.
 *
 * Two different backends behind one editor:
 *   • Welcome message → OF (slot `reply_on_subscribe`). Only one per
 *     account. OF lets us create/edit/delete this exact slot.
 *   • Saved replies → local SQLite (/admin/saved-replies/*). OF's
 *     /messages/templates rejects creates for everything else, so we
 *     persist regular replies in our own DB. They never round-trip
 *     through OF — Composer will read them locally too.
 *
 * The editor itself is identical for both kinds; `draft.kind` switches
 * which mutation we fire on Save.
 */

import { useEffect, useState } from "react";
import { Library, Lock, Paperclip } from "lucide-react";

import { VaultPicker } from "@/components/chat/VaultPicker";
import { TagCreatorsPicker, TaggedCreatorChips, type TaggedCreatorChoice } from "@/components/chat/TagCreatorsPicker";
import { EmojiPickerButton, EmojiQuickRow, insertAtCursor } from "@/components/chat/EmojiBar";
import { GifPickerButton, GifPickerStrip, type PickedGif } from "@/components/chat/GifPicker";
import { Button, Card, Input, Textarea } from "@/components/ui/primitives";
import { AccountChips } from "@/components/AccountChips";
import { useActiveAccounts } from "@/hooks/useAccounts";
import {
  useSavedReplies,
  useCreateSavedReply,
  useDeleteSavedReply,
  useUpdateSavedReply,
} from "@/hooks/useSavedReplies";
import {
  useDeleteWelcome,
  useReplyOnSubscribeFlag,
  useTemplates,
  useToggleReplyOnSubscribe,
  useUpsertWelcome,
} from "@/hooks/useTemplates";
import { RelayError, type OFMessageTemplate, type SavedReply, type VaultMedia } from "@/lib/relay";
import { proxyImage } from "@/lib/mediaUrl";
import { cn } from "@/lib/utils";

/** Pull OF's `error.message` out of the relay's wrapped 4xx/5xx body so
 *  the editor can show a human reason instead of just "HTTP 400". */
function extractError(err: unknown): string {
  if (err instanceof RelayError) {
    const body = err.body as { detail?: { upstream_body?: string } } | undefined;
    const raw = body?.detail?.upstream_body;
    if (typeof raw === "string") {
      try {
        const parsed = JSON.parse(raw) as { error?: { message?: string } };
        if (parsed?.error?.message) return parsed.error.message;
      } catch { /* fall through */ }
    }
    return `HTTP ${err.status} — ${err.message}`;
  }
  return (err as Error)?.message || "unknown error";
}

type DraftKind = "welcome" | "reply";

interface DraftState {
  kind: DraftKind;
  /** Welcome IDs are OF strings; reply IDs are local-DB numeric. */
  id: string | number | null;
  title: string;
  text: string;
  price: string;
  lockedText: boolean;
  attached: VaultMedia[];
  /** Vault ids inside `attached` that ride along UNLOCKED when price > 0. */
  previews: number[];
  /** Creators to @-tag on send. Reply-only — OF welcome templates can't
   *  carry userTags, so the welcome editor keeps this empty. */
  taggedCreators: TaggedCreatorChoice[];
  /** Optional GIF on this template. Reply-only — OF welcome templates
   *  don't carry GIFs through the welcome slot. */
  gif: PickedGif | null;
  /** Script grouping (reply-only). When both `scriptId` and `scriptStep`
   *  are set, the chat composer surfaces this template's successor (same
   *  `scriptId`, `scriptStep + 1`) as a one-tap suggestion after this
   *  template fires. Empty `scriptId` opts out of the script flow. */
  scriptId: string;
  scriptStep: string;
}

const EMPTY_REPLY: DraftState = {
  kind: "reply",
  id: null,
  title: "",
  text: "",
  price: "",
  lockedText: false,
  attached: [],
  previews: [],
  taggedCreators: [],
  gif: null,
  scriptId: "",
  scriptStep: "",
};

const EMPTY_WELCOME: DraftState = { ...EMPTY_REPLY, kind: "welcome" };

function ofMediaToVault(t: OFMessageTemplate): VaultMedia[] {
  return (t.media ?? []).map((m) => ({
    id: m.id,
    type: (m.type as VaultMedia["type"]) || "photo",
    files: m.files ?? null,
  }));
}

function localMediaToVault(r: SavedReply): VaultMedia[] {
  return (r.media ?? []).map((m) => ({
    id: m.id,
    type: (m.type as VaultMedia["type"]) || "photo",
    files: m.files ?? null,
  }));
}

export default function TemplatesTab() {
  const accounts = useActiveAccounts();
  const [accountId, setAccountId] = useState<string | null>(null);
  const [draft, setDraft] = useState<DraftState | null>(null);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [gifPickerOpen, setGifPickerOpen] = useState(false);
  const [saveErr, setSaveErr] = useState<string | null>(null);

  useEffect(() => {
    if (!accountId && accounts.length > 0) setAccountId(accounts[0].id);
  }, [accountId, accounts]);

  // OF welcome (single slot).
  const tplQ = useTemplates(accountId);
  const upsertWelcomeM = useUpsertWelcome(accountId);
  const deleteWelcomeM = useDeleteWelcome(accountId);
  const toggleWelcomeM = useToggleReplyOnSubscribe(accountId);
  // The welcome template's own `isActive` field is decoupled from the master
  // toggle, so read /users/me/settings.replyOnSubscribe to know if OF will
  // actually fire the welcome on new subs.
  const replyOnSubQ = useReplyOnSubscribeFlag(accountId);
  const welcome = (tplQ.data ?? []).find((t) => t.template === "reply_on_subscribe");

  // Local saved replies.
  const replyQ = useSavedReplies(accountId);
  const createReplyM = useCreateSavedReply(accountId);
  const updateReplyM = useUpdateSavedReply(accountId);
  const deleteReplyM = useDeleteSavedReply(accountId);
  const replies = replyQ.data ?? [];

  /** Phone-only: the draft editor mounts at the TOP of the card, so tapping
   *  Edit on a reply far down the list looks like nothing happened. Bail out
   *  at >=768px so desktop scroll behaviour is exactly what it is today. */
  function revealDraft() {
    if (typeof window === "undefined") return;
    if (window.matchMedia("(min-width: 768px)").matches) return;
    requestAnimationFrame(() => {
      document.getElementById("tpl-draft")?.scrollIntoView({ block: "nearest", behavior: "smooth" });
    });
  }

  function startNewReply() {
    setDraft({ ...EMPTY_REPLY });
    setSaveErr(null);
    revealDraft();
  }

  function startNewWelcome() {
    setDraft({ ...EMPTY_WELCOME });
    setSaveErr(null);
    revealDraft();
  }

  function startEditWelcome(t: OFMessageTemplate) {
    setDraft({
      kind: "welcome",
      id: t.id,
      title: "",
      text: stripHtml(t.text),
      price: t.price ? String(t.price) : "",
      lockedText: !!t.lockedText,
      attached: ofMediaToVault(t),
      previews: [],
      taggedCreators: [],
      gif: null,
      scriptId: "",
      scriptStep: "",
    });
    setSaveErr(null);
    revealDraft();
  }

  function startEditReply(r: SavedReply) {
    const attached = localMediaToVault(r);
    const attachedIds = new Set(attached.map((m) => m.id));
    setDraft({
      kind: "reply",
      id: r.id,
      title: r.title ?? "",
      text: r.text,
      price: r.price ? String(r.price) : "",
      lockedText: !!r.locked_text,
      attached,
      previews: (r.previews ?? []).filter((id) => attachedIds.has(id)),
      // The picker resolves the live name/avatar by querying OF when its
      // popover opens; we only persist ids, so seed the chip row with
      // placeholder labels that the user can replace by re-tagging if
      // they care about the display name.
      taggedCreators: (r.tagged_users ?? []).map((id) => ({
        id, name: `user${id}`, username: "", avatar: null,
      })),
      gif: r.gif_id
        ? { id: r.gif_id, thumbUrl: r.gif_url ?? "", fullUrl: r.gif_url ?? "" }
        : null,
      scriptId: r.script_id ?? "",
      scriptStep: r.script_step != null ? String(r.script_step) : "",
    });
    setSaveErr(null);
    revealDraft();
  }

  async function save() {
    if (!draft) return;
    const text = draft.text.trim();
    if (!text) return;
    const price = Number(draft.price) || 0;
    setSaveErr(null);
    try {
      if (draft.kind === "welcome") {
        // OF treats POST /messages/templates/reply_on_subscribe as an
        // upsert — the same call handles both first-time create and
        // subsequent edits, so we don't branch on draft.id here.
        await upsertWelcomeM.mutateAsync({
          text,
          price,
          lockedText: price > 0 ? draft.lockedText : false,
          mediaFiles: draft.attached.map((m) => m.id),
        });
      } else {
        // Filter previews down to ids actually attached so deletions
        // don't leave orphan preview ids that confuse the composer.
        const attachedIds = new Set(draft.attached.map((m) => m.id));
        const previewIds = new Set(
          draft.previews.filter((id) => attachedIds.has(id)),
        );
        // Reorder on save: FREE-marked tiles first, then PPV. The chat
        // composer's preview model is count-based ("leading N tiles are
        // free"), so without this rewrite a user who clicked FREE on the
        // 2nd tile would, on apply, see tile #1 marked FREE and tile #2
        // marked PPV. Stable within each group so the user's manual
        // ordering between free-vs-free / paid-vs-paid is preserved.
        const orderedAttached =
          price > 0 && previewIds.size > 0
            ? [
                ...draft.attached.filter((m) => previewIds.has(m.id)),
                ...draft.attached.filter((m) => !previewIds.has(m.id)),
              ]
            : draft.attached;
        const body = {
          title: draft.title.trim() || null,
          text,
          price,
          lockedText: price > 0 ? draft.lockedText : false,
          media: orderedAttached.map((m) => ({
            id: m.id,
            type: m.type,
            files: m.files ?? null,
          })),
          taggedUsers: draft.taggedCreators.map((c) => c.id),
          previews: price > 0 ? Array.from(previewIds) : [],
          gifId: draft.gif?.id ?? null,
          gifUrl: draft.gif?.thumbUrl ?? draft.gif?.fullUrl ?? null,
          // Only persist the script pair when both halves are present —
          // a script id without a step (or vice versa) can't anchor the
          // next-step lookup, so coerce to null and don't surface it.
          scriptId: draft.scriptId.trim() || null,
          scriptStep:
            draft.scriptId.trim() && /^\d+$/.test(draft.scriptStep.trim())
              ? Number(draft.scriptStep)
              : null,
        };
        if (typeof draft.id === "number") {
          await updateReplyM.mutateAsync({ id: draft.id, ...body });
        } else {
          await createReplyM.mutateAsync(body);
        }
      }
      setDraft(null);
    } catch (err) {
      const msg = extractError(err);
      console.warn("[templates] save failed", err);
      setSaveErr(msg);
    }
  }

  async function removeWelcome() {
    if (!confirm("Delete the welcome message?")) return;
    try { await deleteWelcomeM.mutateAsync(); }
    catch (err) { console.warn("[templates] delete welcome failed", err); }
  }

  async function toggleWelcomeActive(next: boolean) {
    try { await toggleWelcomeM.mutateAsync(next); }
    catch (err) { console.warn("[templates] toggle welcome failed", err); }
  }

  async function removeReply(id: number) {
    if (!confirm("Delete this saved reply?")) return;
    try { await deleteReplyM.mutateAsync(id); }
    catch (err) { console.warn("[templates] delete reply failed", err); }
  }

  const saving =
    upsertWelcomeM.isPending ||
    createReplyM.isPending || updateReplyM.isPending;

  return (
    <div className="space-y-4">
      <Card>
        <div className="flex items-center justify-between gap-3 mb-3 flex-wrap">
          <h2 className="text-base font-semibold">Templates</h2>
          <Button
            size="sm"
            variant="secondary"
            onClick={() => { tplQ.refetch(); replyQ.refetch(); }}
            disabled={tplQ.isFetching || replyQ.isFetching}
          >
            <span className={cn("inline-block mr-1", (tplQ.isFetching || replyQ.isFetching) && "animate-spin")}>↻</span>
            {tplQ.isFetching || replyQ.isFetching ? "Refreshing…" : "Refresh"}
          </Button>
        </div>
        <AccountChips
          accountId={accountId}
          onChange={(id) => { setAccountId(id); setDraft(null); }}
          className="mb-3"
        />

        {(tplQ.error || replyQ.error) && (
          <div className="text-sm text-err mb-3">
            Failed to load: {(tplQ.error || replyQ.error as Error)?.message || "unknown"}
          </div>
        )}

        {draft && (
          <div id="tpl-draft" className="border border-accent/40 rounded-lg p-3 mb-4 space-y-3 bg-bg/40">
            <div className="flex items-center justify-between">
              <div className="text-sm font-medium">
                {draft.id != null
                  ? (draft.kind === "welcome" ? "Edit welcome message" : "Edit saved reply")
                  : (draft.kind === "welcome" ? "New welcome message" : "New saved reply")}
                {draft.kind === "welcome" && (
                  <span className="ml-2 text-[10px] px-1.5 py-0.5 rounded bg-accent/15 text-accent">👋 welcome (OF)</span>
                )}
                {draft.kind === "reply" && (
                  <span className="ml-2 text-[10px] px-1.5 py-0.5 rounded bg-bg-elev-1 text-fg-dim">local</span>
                )}
              </div>
              <button
                type="button"
                onClick={() => setDraft(null)}
                className="text-xs text-fg-dim hover:text-fg"
              >
                Cancel
              </button>
            </div>

            {draft.kind === "reply" && (
              <Input
                type="text"
                value={draft.title}
                onChange={(e) => setDraft((d) => d && { ...d, title: e.target.value })}
                placeholder="Title (optional — shows in picker)"
                className="text-sm"
              />
            )}

            <div className="space-y-1.5">
              <Textarea
                id={draft.kind === "reply" ? "reply-textarea" : "welcome-textarea"}
                value={draft.text}
                onChange={(e) => setDraft((d) => d && { ...d, text: e.target.value })}
                placeholder="Template text"
                className="min-h-24 text-sm font-sans"
              />
              {/* Editor toolbar — emoji is universal; tag-creators only for
               *  local replies since OF welcome templates don't carry
               *  userTags on the template object itself. */}
              <div className="flex items-center gap-1.5 flex-wrap">
                <EmojiQuickRow
                  onInsert={(em) => {
                    const ta = document.getElementById(
                      draft.kind === "reply" ? "reply-textarea" : "welcome-textarea",
                    ) as HTMLTextAreaElement | null;
                    if (ta) {
                      insertAtCursor(ta, draft.text, em, (s) =>
                        setDraft((d) => d && { ...d, text: s }),
                      );
                    } else {
                      setDraft((d) => d && { ...d, text: (d.text || "") + em });
                    }
                  }}
                />
                <EmojiPickerButton
                  align="left"
                  onInsert={(em) => {
                    const ta = document.getElementById(
                      draft.kind === "reply" ? "reply-textarea" : "welcome-textarea",
                    ) as HTMLTextAreaElement | null;
                    if (ta) {
                      insertAtCursor(ta, draft.text, em, (s) =>
                        setDraft((d) => d && { ...d, text: s }),
                      );
                    } else {
                      setDraft((d) => d && { ...d, text: (d.text || "") + em });
                    }
                  }}
                />
                {draft.kind === "reply" && (
                  <GifPickerButton
                    open={gifPickerOpen}
                    onToggle={() => setGifPickerOpen((v) => !v)}
                  />
                )}
                {draft.kind === "reply" && (
                  <TagCreatorsPicker
                    accountId={accountId}
                    selected={draft.taggedCreators}
                    onChange={(next) =>
                      setDraft((d) => d && { ...d, taggedCreators: next })
                    }
                  />
                )}
                {draft.kind === "reply" && draft.taggedCreators.length > 0 && (
                  <div className="flex-1 min-w-0">
                    <TaggedCreatorChips
                      selected={draft.taggedCreators}
                      onChange={(next) =>
                        setDraft((d) => d && { ...d, taggedCreators: next })
                      }
                    />
                  </div>
                )}
              </div>
              {draft.kind === "reply" && gifPickerOpen && (
                <GifPickerStrip
                  accountId={accountId}
                  onClose={() => setGifPickerOpen(false)}
                  onPick={(g) => {
                    setDraft((d) => d && { ...d, gif: g });
                    setGifPickerOpen(false);
                  }}
                />
              )}
              {draft.kind === "reply" && draft.gif && (
                <div className="flex items-center gap-2 bg-bg/60 border border-border rounded-md px-2 py-2">
                  <span className="text-[10px] uppercase tracking-wide text-fg-dim">gif</span>
                  <div className="relative w-14 h-14 rounded-md overflow-hidden border border-border bg-bg-elev-1">
                    {/* eslint-disable-next-line @next/next/no-img-element */}
                    <img
                      src={draft.gif.thumbUrl || draft.gif.fullUrl}
                      alt={draft.gif.title || "GIF"}
                      className="w-full h-full object-cover"
                    />
                  </div>
                  <button
                    type="button"
                    onClick={() => setDraft((d) => d && { ...d, gif: null })}
                    className="text-[11px] text-fg-dim hover:text-fg ml-auto"
                  >
                    Remove GIF
                  </button>
                </div>
              )}
            </div>

            <div className="flex items-center gap-2 flex-wrap">
              {draft.attached.map((m) => {
                const rawThumb =
                  m.files?.thumb?.url ||
                  m.files?.squarePreview?.url ||
                  m.files?.preview?.url ||
                  null;
                const thumb = proxyImage(rawThumb, accountId);
                const isPpv = Number(draft.price) > 0;
                const isFree = isPpv && draft.previews.includes(m.id);
                return (
                  <div
                    key={m.id}
                    className="relative w-14 h-14 rounded-md overflow-hidden border border-border bg-bg-elev-1 group"
                  >
                    {thumb ? (
                      <img src={thumb} alt="" loading="lazy" decoding="async" className="w-full h-full object-cover" />
                    ) : (
                      <div className="w-full h-full grid place-items-center text-[10px] text-fg-dim">
                        {m.type}
                      </div>
                    )}
                    {/* FREE/PPV toggle — click to flip whether this tile is
                     *  unlocked when the template fires at price > 0. The
                     *  reply editor only; OF welcome templates ignore
                     *  per-media preview flags. */}
                    {isPpv && draft.kind === "reply" && (
                      <button
                        type="button"
                        onClick={() =>
                          setDraft((d) => {
                            if (!d) return d;
                            const has = d.previews.includes(m.id);
                            return {
                              ...d,
                              previews: has
                                ? d.previews.filter((x) => x !== m.id)
                                : [...d.previews, m.id],
                            };
                          })
                        }
                        title={isFree ? "Unlocked preview — click to lock" : "PPV-locked — click to unlock as preview"}
                        className={cn(
                          "absolute bottom-0 left-0 right-0 text-[9px] font-bold leading-tight text-white py-0.5 text-center",
                          isFree ? "bg-fg-dim" : "bg-err",
                        )}
                      >
                        {isFree ? "FREE" : "PPV"}
                      </button>
                    )}
                    <button
                      type="button"
                      onClick={() =>
                        setDraft((d) => {
                          if (!d) return d;
                          return {
                            ...d,
                            attached: d.attached.filter((x) => x.id !== m.id),
                            previews: d.previews.filter((x) => x !== m.id),
                          };
                        })
                      }
                      className="absolute top-0.5 right-0.5 w-4 h-4 max-md:w-6 max-md:h-6 rounded-full bg-black/70 text-white grid place-items-center text-[10px] max-md:text-xs opacity-80 hover:opacity-100"
                      aria-label="Remove attachment"
                    >
                      ✕
                    </button>
                  </div>
                );
              })}
              <button
                type="button"
                onClick={() => setPickerOpen(true)}
                disabled={!accountId}
                className="w-14 h-14 rounded-md border border-dashed border-border bg-transparent hover:bg-bg-elev-1 text-fg-dim hover:text-fg grid place-items-center text-xs gap-0.5"
                title="Attach media from vault"
              >
                <Library size={18} aria-hidden />
                <span aria-hidden>+</span>
              </button>
            </div>
            {draft.kind === "reply" && Number(draft.price) > 0 && draft.attached.length > 0 && (
              <div className="text-[10px] text-fg-dim -mt-1">
                Click FREE/PPV on a tile to choose which media unlocks before the fan pays.
                {draft.previews.length === 0 && " · all locked (fan pays to see any)"}
              </div>
            )}

            {draft.kind === "reply" && (
              <div className="flex items-center gap-2 flex-wrap text-xs text-fg-dim">
                <span title="Group templates into an ordered sequence. The chat composer suggests the next step after this one fires.">
                  Script
                </span>
                <Input
                  type="text"
                  value={draft.scriptId}
                  onChange={(e) => setDraft((d) => d && { ...d, scriptId: e.target.value })}
                  placeholder="name (e.g. welcome-flow)"
                  className="w-44 py-1 text-sm"
                />
                <span>step</span>
                <Input
                  type="text"
                  inputMode="numeric"
                  value={draft.scriptStep}
                  onChange={(e) =>
                    setDraft((d) =>
                      d && { ...d, scriptStep: e.target.value.replace(/[^\d]/g, "") },
                    )
                  }
                  placeholder="1"
                  className="w-16 py-1 text-sm"
                  disabled={!draft.scriptId.trim()}
                />
                {draft.scriptId.trim() && !/^\d+$/.test(draft.scriptStep.trim()) && (
                  <span className="text-warn">step needed to enable the next-step bubble</span>
                )}
              </div>
            )}

            <div className="flex items-center gap-4 flex-wrap">
              <label className="text-xs text-fg-dim flex items-center gap-2">
                Price $
                <Input
                  type="text"
                  inputMode="decimal"
                  value={draft.price}
                  onChange={(e) => setDraft((d) => d && { ...d, price: e.target.value })}
                  className="w-24 py-1 text-sm"
                  placeholder="0.00"
                />
              </label>
              <label className="text-xs text-fg-dim flex items-center gap-1.5 cursor-pointer select-none">
                <input
                  type="checkbox"
                  checked={draft.lockedText}
                  disabled={!(Number(draft.price) > 0)}
                  onChange={(e) => setDraft((d) => d && { ...d, lockedText: e.target.checked })}
                  className="accent-accent"
                />
                Lock text behind price
              </label>
              <div className="ml-auto flex items-center gap-2">
                <Button
                  size="sm"
                  onClick={save}
                  disabled={!draft.text.trim() || saving}
                >
                  {saving ? "Saving…" : "Save"}
                </Button>
              </div>
            </div>

            {saveErr && (
              <div className="text-[11px] px-2.5 py-1.5 rounded-md border bg-err/10 border-err/30 text-err">
                ⚠ Save failed: {saveErr}
              </div>
            )}
          </div>
        )}

        <section className="space-y-2">
          <div className="flex items-center justify-between">
            <h3 className="text-xs font-medium text-fg-dim uppercase tracking-wide">Welcome message</h3>
            {!welcome && draft?.kind !== "welcome" && (
              <Button size="sm" variant="ghost" onClick={startNewWelcome}>+ Set welcome</Button>
            )}
          </div>
          {welcome ? (
            <WelcomeRow
              t={welcome}
              accountId={accountId}
              active={!!replyOnSubQ.data}
              onEdit={startEditWelcome}
              onDelete={removeWelcome}
              onToggleActive={toggleWelcomeActive}
              toggling={toggleWelcomeM.isPending || replyOnSubQ.isLoading}
              busy={deleteWelcomeM.isPending}
            />
          ) : (
            <div className="text-xs text-fg-dim italic">No welcome message set — new subscribers won't get an auto-reply.</div>
          )}
        </section>

        <section className="space-y-2 mt-6">
          <div className="flex items-center justify-between">
            <h3 className="text-xs font-medium text-fg-dim uppercase tracking-wide">
              Saved replies <span className="text-fg-dim normal-case font-normal">(local)</span>
            </h3>
            <Button size="sm" variant="ghost" onClick={startNewReply}>+ Add</Button>
          </div>
          {replyQ.isLoading && replies.length === 0 && (
            <div className="text-sm text-fg-dim">Loading…</div>
          )}
          {!replyQ.isLoading && replies.length === 0 && (
            <div className="text-xs text-fg-dim italic">No saved replies yet. They live in Fastt only — OF won't see them.</div>
          )}
          {replies.map((r) => (
            <ReplyRow
              key={r.id}
              r={r}
              accountId={accountId}
              onEdit={startEditReply}
              onDelete={() => removeReply(r.id)}
              busy={deleteReplyM.isPending && deleteReplyM.variables === r.id}
            />
          ))}
        </section>
      </Card>

      <VaultPicker
        open={pickerOpen}
        onClose={() => setPickerOpen(false)}
        accountId={accountId}
        initialSelectedIds={draft?.attached.map((m) => m.id) ?? []}
        onConfirm={(picked) => {
          // Fresh-claim dicts (negative ids) are ephemeral and useless for
          // persistence — drop them. The numeric vault ids work for both
          // OF welcome msg and local saved replies.
          const vaultOnly = picked.filter((m) => m.id > 0);
          const stillAttached = new Set(vaultOnly.map((m) => m.id));
          setDraft((d) =>
            d && {
              ...d,
              attached: vaultOnly,
              previews: d.previews.filter((id) => stillAttached.has(id)),
            },
          );
        }}
      />
    </div>
  );
}

function WelcomeRow({
  t, accountId, active, onEdit, onDelete, onToggleActive, toggling, busy,
}: {
  t: OFMessageTemplate;
  accountId: string | null;
  active: boolean;
  onEdit: (t: OFMessageTemplate) => void;
  onDelete: () => void;
  onToggleActive: (next: boolean) => void;
  toggling: boolean;
  busy: boolean;
}) {
  const media = t.media ?? [];
  return (
    <div className={cn(
      "border border-border rounded-md px-3 py-2.5 flex items-start gap-3",
      busy && "opacity-60",
    )}>
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2 mb-1 flex-wrap">
          <span className="text-[10px] px-1.5 py-0.5 rounded bg-accent/15 text-accent">👋 welcome</span>
          <span
            className={cn(
              "text-[10px] px-1.5 py-0.5 rounded",
              active ? "bg-ok/15 text-ok" : "bg-bg-elev-1 text-fg-dim",
            )}
            title={active ? "OF will fire this on new subscribers" : "Stored but OF won't fire it — toggle on to enable"}
          >
            {active ? "● on" : "○ off"}
          </span>
          {(t.mediaCount ?? 0) > 0 && (
            <span className="text-[10px] text-fg-dim inline-flex items-center gap-1">
              <Paperclip size={12} aria-hidden /> {t.mediaCount}
            </span>
          )}
          {t.price && t.price > 0 && (
            <span className="text-[10px] text-warn inline-flex items-center gap-1">
              <Lock size={12} aria-hidden /> ${t.price.toFixed(2)}
            </span>
          )}
        </div>
        <div className="text-sm whitespace-pre-wrap break-words line-clamp-4">
          {stripHtml(t.displayText || t.text)}
        </div>
        {media.length > 0 && <MediaStrip media={media} accountId={accountId} />}
      </div>
      <div className="flex flex-col gap-1 shrink-0">
        <Button
          size="sm"
          variant={active ? "secondary" : "primary"}
          onClick={() => onToggleActive(!active)}
          disabled={toggling}
          title={active ? "Disable welcome auto-reply" : "Enable welcome auto-reply"}
        >
          {toggling ? "…" : active ? "Disable" : "Enable"}
        </Button>
        <Button size="sm" variant="secondary" onClick={() => onEdit(t)}>Edit</Button>
        <Button size="sm" variant="danger" onClick={onDelete} disabled={busy}>Delete</Button>
      </div>
    </div>
  );
}

function ReplyRow({
  r, accountId, onEdit, onDelete, busy,
}: {
  r: SavedReply;
  accountId: string | null;
  onEdit: (r: SavedReply) => void;
  onDelete: () => void;
  busy: boolean;
}) {
  const media = r.media ?? [];
  return (
    <div className={cn(
      "border border-border rounded-md px-3 py-2.5 flex items-start gap-3",
      busy && "opacity-60",
    )}>
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2 mb-1">
          {r.title && (
            <span className="text-[11px] font-medium text-fg">{r.title}</span>
          )}
          {media.length > 0 && (
            <span className="text-[10px] text-fg-dim inline-flex items-center gap-1">
              <Paperclip size={12} aria-hidden /> {media.length}
            </span>
          )}
          {r.price > 0 && (
            <span className="text-[10px] text-warn inline-flex items-center gap-1">
              <Lock size={12} aria-hidden /> ${r.price.toFixed(2)}
            </span>
          )}
        </div>
        <div className="text-sm whitespace-pre-wrap break-words line-clamp-4">
          {r.text}
        </div>
        {media.length > 0 && <MediaStrip media={media} accountId={accountId} />}
      </div>
      <div className="flex flex-col gap-1 shrink-0">
        <Button size="sm" variant="secondary" onClick={() => onEdit(r)}>Edit</Button>
        <Button size="sm" variant="danger" onClick={onDelete} disabled={busy}>Delete</Button>
      </div>
    </div>
  );
}

function MediaStrip({
  media, accountId,
}: {
  media: Array<{ id: number; type?: string; files?: SavedReply["media"][number]["files"] }>;
  accountId: string | null;
}) {
  return (
    <div className="flex items-center gap-1.5 mt-2 flex-wrap">
      {media.slice(0, 6).map((m) => {
        const rawThumb =
          m.files?.thumb?.url ||
          m.files?.squarePreview?.url ||
          m.files?.preview?.url ||
          null;
        const thumb = proxyImage(rawThumb, accountId);
        return (
          <div key={m.id} className="w-10 h-10 rounded border border-border overflow-hidden bg-bg-elev-1">
            {thumb ? (
              <img src={thumb} alt="" loading="lazy" decoding="async" className="w-full h-full object-cover" />
            ) : (
              <div className="w-full h-full grid place-items-center text-[9px] text-fg-dim">
                {m.type ?? "?"}
              </div>
            )}
          </div>
        );
      })}
      {media.length > 6 && (
        <span className="text-[10px] text-fg-dim">+{media.length - 6}</span>
      )}
    </div>
  );
}

function stripHtml(s: string): string {
  return s
    .replace(/<br\s*\/?>/gi, "\n")
    .replace(/<\/p\s*>/gi, "\n")
    .replace(/<[^>]+>/g, "")
    .replace(/&nbsp;/g, " ")
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, "\"")
    .replace(/&#39;/g, "'")
    .trim();
}
