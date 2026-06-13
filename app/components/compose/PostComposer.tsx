"use client";

/**
 * PostComposer — modal for creating a public feed post via
 * POST /api/of/v2/posts. Mirrors OF's own "Create post" surface in the
 * minimum-viable shape:
 *   • text body
 *   • media from vault (reuses the chat VaultPicker — same picker, no
 *     fan-scope so the per-fan badges/MRU just stay neutral)
 *   • price (0 = free; >0 = PPV)
 *
 * Deliberately deferred to v2:
 *   • Polls — no captured curl yet; would be guesswork.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { useActiveAccounts } from "@/hooks/useAccounts";
import { useAllModelsInclude } from "@/hooks/useAllModelsInclude";
import { relay, type VaultMedia } from "@/lib/relay";
import { VaultPicker } from "@/components/chat/VaultPicker";
import { EmojiPickerButton, EmojiQuickRow, insertAtCursor } from "@/components/chat/EmojiBar";
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

interface CreatePostResp {
  id?: number;
  // OF returns the full post shape; we only need a success signal so the
  // rest stays loose.
  [k: string]: unknown;
}

export function PostComposer({ open, onClose }: { open: boolean; onClose: () => void }) {
  const qc = useQueryClient();
  const activeAccounts = useActiveAccounts();
  // Shared include set with ScopeSwitcher's all-models aggregate — a model
  // un-checked there is also omitted from a broadcast here.
  const { isIncluded, toggle: toggleAllModelsInclude } = useAllModelsInclude();
  const [allModels, setAllModels] = useState(false);
  const [accountId, setAccountId] = useState<string | null>(null);
  const [text, setText] = useState("");
  const [attached, setAttached] = useState<VaultMedia[]>([]);
  const [price, setPrice] = useState<string>("");
  const [schedule, setSchedule] = useState<string>("");
  const [pickerOpen, setPickerOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [progress, setProgress] = useState<string | null>(null);
  const [results, setResults] = useState<FanOutResult[] | null>(null);
  const [taggedCreators, setTaggedCreators] = useState<TaggedCreatorChoice[]>([]);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);

  // All-models mode is text-only for now (upload-from-computer is paused
  // pending a producer-pipeline fix). Drop vault attachments when entering
  // it — vault ids don't roundtrip across accounts. Also force no tags:
  // per-model tag lists differ on OF, so a tag that's only valid on one
  // creator's friend-list 400s on the rest.
  useEffect(() => {
    if (allModels) {
      setAccountId(null);
      setAttached([]);
      setTaggedCreators([]);
    }
  }, [allModels]);

  async function postOne(
    forAccountId: string,
    mediaFiles: Array<number | Record<string, unknown>>,
    scheduledIso: string | null,
  ) {
    const priceNum = price ? Number(price) : 0;
    const body: Record<string, unknown> = {
      text: text.trim(),
      media_files: mediaFiles,
      price: priceNum,
      posted_at: scheduledIso,
    };
    if (taggedCreators.length > 0) {
      body.tagged_users = taggedCreators.map((c) => c.id);
    }
    const resp = await relay.post<CreatePostResp>(
      "/api/of/v2/posts",
      body,
      { accountId: forAccountId },
    );
    if (schedule) recordSchedule(forAccountId, schedule);
    return resp;
  }

  // Honour the shared include set from ScopeSwitcher: models the user
  // unchecked there are also dropped from the broadcast fan-out.
  const broadcastAccounts = useMemo(
    () => activeAccounts.filter((a) => isIncluded(a.id)),
    [activeAccounts, isIncluded],
  );
  const allAccountIds = useMemo(() => activeAccounts.map((a) => a.id), [activeAccounts]);

  const create = useMutation({
    mutationFn: async () => {
      const trimmed = text.trim();
      if (allModels) {
        if (broadcastAccounts.length === 0) throw new Error("No models picked for broadcast");
      } else {
        if (!accountId) throw new Error("Pick an account");
      }
      const totalMedia = allModels ? 0 : attached.length;
      if (!trimmed && totalMedia === 0) throw new Error("Add text or media");
      const priceNum = price ? Number(price) : 0;
      if (!Number.isFinite(priceNum) || priceNum < 0) throw new Error("Invalid price");
      const scheduledIso = schedule ? localDatetimeToIso(schedule) : null;
      if (schedule && !scheduledIso) throw new Error("Invalid schedule date");

      if (allModels) {
        const accountIds = broadcastAccounts.map((a) => a.id);
        setProgress(`Posting to 0 / ${accountIds.length}…`);
        const fanResults: FanOutResult[] = [];
        for (let i = 0; i < accountIds.length; i++) {
          const aid = accountIds[i];
          try {
            await postOne(aid, [], scheduledIso);
            fanResults.push({ accountId: aid, ok: true });
          } catch (e) {
            fanResults.push({ accountId: aid, ok: false, error: (e as Error).message });
          }
          setProgress(`Posting to ${i + 1} / ${accountIds.length}…`);
        }
        setResults(fanResults);
        setProgress(null);
        if (fanResults.every((r) => r.ok)) return fanResults;
        throw new Error(summarizeFanOut(fanResults));
      }

      const mediaFiles: Array<number | Record<string, unknown>> = attached.map(
        (m) => m._claim ?? m.id,
      );
      return postOne(accountId!, mediaFiles, scheduledIso);
    },
    onSuccess: () => {
      // The wall-media query backs the vault picker's blue ring; nuking
      // it ensures the freshly-posted media flips state on next open.
      qc.invalidateQueries({ queryKey: ["wall-media"] });
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
    setSchedule("");
    setError(null);
    setProgress(null);
    setResults(null);
    setAllModels(false);
    setTaggedCreators([]);
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
    // Snapshot semantics — replace attachments wholesale so the saved
    // ordering survives the pick.
    setText(t.text);
    setAttached(templateMediaToVault(t));
    if (t.price > 0) {
      setPrice(String(t.price));
    }
    if (t.taggedUsers.length > 0) {
      setTaggedCreators((prev) => {
        const have = new Set(prev.map((c) => c.id));
        const additions: TaggedCreatorChoice[] = t.taggedUsers
          .filter((id) => !have.has(id))
          .map((id) => ({ id, name: `user${id}`, username: "", avatar: null }));
        return [...prev, ...additions];
      });
    }
    setTimeout(() => textareaRef.current?.focus(), 0);
  }

  if (!open) return null;

  const priceNum = price ? Number(price) : 0;
  const isPPV = priceNum > 0;
  const isScheduled = !!schedule;

  return (
    <div
      className="fixed inset-0 z-50 bg-black/50 grid place-items-center p-4"
      onClick={() => { if (!create.isPending) { reset(); onClose(); } }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="w-full max-w-[560px] max-h-[90vh] flex flex-col bg-panel border border-border rounded-xl shadow-2xl"
      >
        <header className="px-4 py-3 border-b border-border flex items-center justify-between">
          <h2 className="text-sm font-semibold">New post</h2>
          <button
            type="button"
            onClick={() => { if (!create.isPending) { reset(); onClose(); } }}
            className="text-fg-dim hover:text-fg text-lg leading-none"
            title="Close"
          >×</button>
        </header>

        <div className="flex-1 overflow-y-auto p-4 space-y-3">
          <label className="flex items-center gap-2 text-xs cursor-pointer select-none">
            <input
              type="checkbox"
              checked={allModels}
              onChange={(e) => setAllModels(e.target.checked)}
            />
            <span className="font-medium">Post from ALL models</span>
            <span className="text-fg-dim">
              ({broadcastAccounts.length}/{activeAccounts.length} included)
            </span>
          </label>

          {allModels ? (
            <>
              <div className="text-[11px] text-fg-dim border border-dashed border-border rounded-md py-2 px-3">
                All-models posts are text-only for now — uploads from
                computer are temporarily disabled.
              </div>
              {/* Creator picker — click a chip to drop / re-add a model
               *  from the broadcast (and from the all-models aggregate
               *  everywhere else). Mirrors MassMessageComposer. */}
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
              placeholder="What's on your feed today?"
              rows={5}
              className="w-full bg-bg border border-border rounded-md px-3 py-2 text-sm placeholder:text-muted focus:outline-none focus:border-accent resize-y"
            />
            <div className="flex items-center gap-1.5 flex-wrap">
              <EmojiQuickRow onInsert={onEmoji} />
              <EmojiPickerButton align="left" onInsert={onEmoji} />
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
              price={price ? Number(price) || 0 : 0}
            />
          )}

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
              {isPPV ? `🔒 PPV $${priceNum.toFixed(2)}` : "free"}
            </span>
          </div>

          <ScheduleField
            scope={allModels ? "all-models" : accountId}
            value={schedule}
            onChange={setSchedule}
          />

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
            disabled={create.isPending}
            className="text-xs px-3 py-1.5 rounded border border-border hover:border-border-light text-fg-dim hover:text-fg disabled:opacity-50"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={() => {
              setError(null);
              setResults(null);
              if (allModels && !confirm(
                `Post from ${broadcastAccounts.length} model${broadcastAccounts.length === 1 ? "" : "s"}?`,
              )) return;
              create.mutate();
            }}
            disabled={
              create.isPending ||
              (allModels ? broadcastAccounts.length === 0 : !accountId)
            }
            className="text-xs px-4 py-1.5 rounded bg-accent text-white font-medium hover:bg-accent-hover disabled:opacity-50"
          >
            {create.isPending
              ? (isScheduled ? "Scheduling…" : "Posting…")
              : (isScheduled ? "Schedule post" : "Post")}
          </button>
        </footer>
      </div>

      <VaultPicker
        open={pickerOpen}
        onClose={() => setPickerOpen(false)}
        accountId={accountId}
        fanId={null}
        initialSelectedIds={attached.map((m) => m.id)}
        onConfirm={(picked) => {
          setAttached(picked);
          setPickerOpen(false);
        }}
      />
    </div>
  );
}
