"use client";

/**
 * FunnelMediaPane — map ONE model's vault media to a (global) funnel.
 *
 * A funnel's TEXT (opener + step copy/prompts/price) is shared across all of an
 * owner's models. Its MEDIA is NOT: an OnlyFans vault id is per-account, so an
 * id picked on model A is meaningless in model B's vault. This pane edits the
 * per-account binding (funnel_account_media) for the CURRENTLY selected model —
 * the funnel's opener media + each PPV step's media — and saves it via
 * useSaveFunnelMedia. To map another model, switch the model chip (one row per
 * account).
 *
 * It reads the funnel's SAVED steps (useFunnel) to know which steps are PPV; add
 * a step + save the funnel text first, then attach its media here.
 */

import { useEffect, useMemo, useState } from "react";

import { Button, Card, Input } from "@/components/ui/primitives";
import { VaultPicker } from "@/components/chat/VaultPicker";
import { useActiveAccounts } from "@/hooks/useAccounts";
import { useVaultMediaByIds } from "@/hooks/useVaultMediaByIds";
import { type VaultMedia } from "@/lib/relay";
import { proxyImage } from "@/lib/mediaUrl";
import {
  useFunnel,
  useFunnelMedia,
  useSaveFunnelMedia,
  type FunnelSummary,
  type PpvStep,
} from "@/hooks/useFunnels";

/** Local draft for one PPV step's media (keyed by step number elsewhere). */
interface StepDraft {
  ids: number[];
  /** How many of the leading picked media go out FREE as the teaser. */
  previewCount: number;
}

/** A PPV step in the funnel that needs media, with its 1-based step number. */
interface PpvSlot {
  stepNo: number;
  label: string;
}

export function FunnelMediaPane({
  funnel,
  accountId,
  onClose,
}: {
  funnel: FunnelSummary;
  accountId: string | null;
  onClose: () => void;
}) {
  const accounts = useActiveAccounts();
  const accountName =
    accounts.find((a) => a.id === accountId)?.nickname ?? accountId ?? "";

  const fullQ = useFunnel(funnel.id);
  const mediaQ = useFunnelMedia(funnel.id, accountId);
  const saveM = useSaveFunnelMedia();

  const [openerIds, setOpenerIds] = useState<number[]>([]);
  const [steps, setSteps] = useState<Record<string, StepDraft>>({});
  const [pickerFor, setPickerFor] = useState<"opener" | string | null>(null);
  const [seeded, setSeeded] = useState(false);
  const [done, setDone] = useState(false);

  // The funnel's PPV steps (the only ones that carry media), in order.
  const ppvSlots = useMemo<PpvSlot[]>(() => {
    const out: PpvSlot[] = [];
    (fullQ.data?.steps ?? []).forEach((s, i) => {
      if (s.type === "paid_ppv") {
        const stepNo = typeof s.step === "number" ? s.step : i + 1;
        out.push({ stepNo, label: `Step ${stepNo} · PPV` });
      }
    });
    return out;
  }, [fullQ.data]);

  // Seed once from the saved binding + the funnel's steps.
  useEffect(() => {
    if (seeded || !fullQ.data || !mediaQ.data) return;
    setOpenerIds((mediaQ.data.opening_media_ids ?? []).filter((n) => n > 0));
    const next: Record<string, StepDraft> = {};
    for (const slot of ppvSlots) {
      const m = mediaQ.data.steps_media?.[String(slot.stepNo)];
      const ids = (m?.media_files ?? []).filter((n) => n > 0);
      const previewCount = Math.min((m?.previews ?? []).length, ids.length);
      next[String(slot.stepNo)] = { ids, previewCount };
    }
    setSteps(next);
    setSeeded(true);
  }, [seeded, fullQ.data, mediaQ.data, ppvSlots]);

  function patchStep(stepNo: number, patch: Partial<StepDraft>) {
    setDone(false);
    setSteps((prev) => ({
      ...prev,
      [String(stepNo)]: { ...(prev[String(stepNo)] ?? { ids: [], previewCount: 0 }), ...patch },
    }));
  }

  /** Copy one PPV step's media + teaser count onto every OTHER PPV step (a common
   *  ask when the same set sells across steps). Teaser is re-clamped per target. */
  function copyToAllPpv(fromStepNo: number) {
    const src = steps[String(fromStepNo)];
    if (!src || src.ids.length === 0) return;
    setDone(false);
    setSteps((prev) => {
      const next = { ...prev };
      for (const slot of ppvSlots) {
        if (slot.stepNo === fromStepNo) continue;
        next[String(slot.stepNo)] = {
          ids: [...src.ids],
          previewCount: Math.min(src.previewCount, src.ids.length),
        };
      }
      return next;
    });
  }

  // Resolve every picked id (opener + all steps) → full VaultMedia for thumbnails.
  // Bare ids carry no image URL, so we fan out a by-id vault read (cached,
  // 404-tolerant); an unresolved id falls back to a "#id" badge. One hook for the
  // whole pane keeps the queries shared/deduped.
  const allIds = useMemo(
    () => [...openerIds, ...Object.values(steps).flatMap((d) => d.ids)],
    [openerIds, steps],
  );
  const resolvedById = useVaultMediaByIds(accountId, allIds);
  function thumbFor(id: number): string {
    const f = resolvedById[id]?.files;
    const raw = f?.thumb?.url || f?.squarePreview?.url || f?.preview?.url || null;
    return raw ? proxyImage(raw, accountId) : "";
  }

  /** Move the item at `i` one slot left (-1) or right (+1), preserving the rest.
   *  Order matters: the send goes out in this order and the first `previewCount`
   *  are the FREE teaser, so reordering picks which images are free. */
  function reorder(ids: number[], i: number, dir: -1 | 1): number[] {
    const j = i + dir;
    if (j < 0 || j >= ids.length) return ids;
    const next = [...ids];
    [next[i], next[j]] = [next[j], next[i]];
    return next;
  }
  function moveOpener(i: number, dir: -1 | 1) {
    setDone(false);
    setOpenerIds((prev) => reorder(prev, i, dir));
  }
  function moveStep(stepNo: number, i: number, dir: -1 | 1) {
    patchStep(stepNo, { ids: reorder(steps[String(stepNo)]?.ids ?? [], i, dir) });
  }
  function removeOpener(i: number) {
    setDone(false);
    setOpenerIds((prev) => prev.filter((_, k) => k !== i));
  }
  function removeStep(stepNo: number, i: number) {
    const d = steps[String(stepNo)];
    if (!d) return;
    const ids = d.ids.filter((_, k) => k !== i);
    patchStep(stepNo, { ids, previewCount: Math.min(d.previewCount, ids.length) });
  }

  async function save() {
    if (!accountId) return;
    setDone(false);
    const steps_media: Record<string, { media_files: number[]; previews: number[] }> = {};
    for (const slot of ppvSlots) {
      const d = steps[String(slot.stepNo)];
      if (!d || d.ids.length === 0) continue; // empty → omit (server drops it too)
      steps_media[String(slot.stepNo)] = {
        media_files: d.ids,
        previews: d.ids.slice(0, Math.min(d.previewCount, d.ids.length)),
      };
    }
    await saveM.mutateAsync({
      funnelId: funnel.id,
      accountId,
      opening_media_ids: openerIds,
      steps_media,
    });
    setDone(true);
  }

  // What the open picker is currently editing → its selected ids.
  const pickerIds =
    pickerFor === "opener"
      ? openerIds
      : pickerFor != null
        ? steps[pickerFor]?.ids ?? []
        : [];

  if (!accountId) {
    return (
      <Card className="p-4 text-sm text-amber-500 border-accent/40">
        Pick a model (account) in the chips above to map its vault media.
      </Card>
    );
  }

  return (
    <Card className="p-4 space-y-3 border-accent/40">
      <div className="flex items-center justify-between gap-3">
        <h3 className="text-sm font-semibold text-fg">
          Funnel media · {funnel.name}{" "}
          <span className="text-fg-dim font-normal">for {accountName}</span>
        </h3>
        <button
          type="button"
          onClick={onClose}
          className="text-fg-dim hover:text-fg text-sm"
          aria-label="Close media editor"
        >
          ✕
        </button>
      </div>

      <p className="text-[11px] text-fg-dim">
        Vault media is per model — these ids are picked from{" "}
        <span className="text-fg">{accountName}</span>&apos;s vault. The funnel
        text is shared; switch the model to map another one&apos;s media.
      </p>

      {fullQ.isLoading || mediaQ.isLoading ? (
        <div className="text-sm text-fg-dim">Loading media…</div>
      ) : (
        <div className="space-y-3">
          {/* Opener media */}
          <div className="space-y-1.5">
            <MediaRow
              title="Opening message media"
              hint="Sent with the funnel opener (optional)."
              ids={openerIds}
              onPick={() => setPickerFor("opener")}
              onClear={() => { setDone(false); setOpenerIds([]); }}
            />
            {openerIds.length > 0 && (
              <MediaStrip
                ids={openerIds}
                thumbFor={thumbFor}
                onMove={(i, dir) => moveOpener(i, dir)}
                onRemove={(i) => removeOpener(i)}
              />
            )}
          </div>

          {/* PPV step media */}
          {ppvSlots.length === 0 ? (
            <div className="text-[11px] text-fg-dim italic">
              This funnel has no PPV steps yet. Add a PPV step in the funnel
              editor (and save), then attach its media here.
            </div>
          ) : (
            ppvSlots.map((slot) => {
              const d = steps[String(slot.stepNo)] ?? { ids: [], previewCount: 0 };
              const free = Math.min(d.previewCount, d.ids.length);
              return (
                <div key={slot.stepNo} className="space-y-1.5">
                  <MediaRow
                    title={slot.label}
                    hint="The paywalled vault media for this PPV step."
                    ids={d.ids}
                    onPick={() => setPickerFor(String(slot.stepNo))}
                    // Split clear: this wipes the MEDIA (and its teaser); the
                    // "clear teaser" link below resets only the free-preview count.
                    onClear={() => patchStep(slot.stepNo, { ids: [], previewCount: 0 })}
                    clearLabel="clear media"
                  />
                  {d.ids.length > 0 && (
                    <>
                      <label className="block text-[11px] text-fg-dim pl-1">
                        Free preview — first
                        <Input
                          type="number"
                          min={0}
                          max={d.ids.length}
                          value={String(d.previewCount)}
                          onChange={(e) => {
                            const n = Math.max(0, Math.min(Number(e.target.value) || 0, d.ids.length));
                            patchStep(slot.stepNo, { previewCount: n });
                          }}
                          className="inline-block w-16 mx-1 align-middle"
                        />
                        of {d.ids.length}{" "}
                        <span className="text-fg-dim/70">(max {d.ids.length})</span> shown
                        free as a teaser
                        {free === 0 && " — fully locked"}
                        {free > 0 && (
                          <button
                            type="button"
                            onClick={() => patchStep(slot.stepNo, { previewCount: 0 })}
                            className="ml-2 text-fg-dim hover:text-err underline"
                          >
                            clear teaser
                          </button>
                        )}
                      </label>

                      {/* Thumbnail strip: the leading `free` images go out UNLOCKED
                          (🔓), the rest stay paywalled (🔒). Reorder with ◀ ▶ to pick
                          which ones are the free teaser (it's always the FIRST N). */}
                      <MediaStrip
                        ids={d.ids}
                        free={free}
                        thumbFor={thumbFor}
                        onMove={(i, dir) => moveStep(slot.stepNo, i, dir)}
                        onRemove={(i) => removeStep(slot.stepNo, i)}
                      />

                      {ppvSlots.length > 1 && (
                        <button
                          type="button"
                          onClick={() => copyToAllPpv(slot.stepNo)}
                          className="pl-1 text-[11px] text-accent hover:underline"
                        >
                          ⧉ Copy media to all PPV steps
                        </button>
                      )}
                    </>
                  )}
                </div>
              );
            })
          )}
        </div>
      )}

      <div className="flex items-center gap-2 pt-1">
        <Button size="sm" variant="primary" onClick={save} disabled={saveM.isPending}>
          {saveM.isPending ? "Saving…" : "Save media"}
        </Button>
        <Button size="sm" variant="ghost" onClick={onClose} disabled={saveM.isPending}>
          Close
        </Button>
        {done && <span className="text-xs text-emerald-500">Saved for {accountName}.</span>}
        {saveM.isError && (
          <span className="text-xs text-err">
            {(saveM.error as Error)?.message || "Save failed"}
          </span>
        )}
      </div>

      <VaultPicker
        open={pickerFor !== null}
        onClose={() => setPickerFor(null)}
        accountId={accountId}
        fanId={null}
        initialSelectedIds={pickerIds}
        onConfirm={(picked: VaultMedia[]) => {
          const ids = picked
            .map((m) => m.id)
            .filter((id): id is number => typeof id === "number" && id > 0);
          if (pickerFor === "opener") {
            setOpenerIds(ids);
          } else if (pickerFor != null) {
            const stepNo = Number(pickerFor);
            const prev = steps[pickerFor]?.previewCount ?? 0;
            patchStep(stepNo, { ids, previewCount: Math.min(prev, ids.length) });
          }
          setDone(false);
          setPickerFor(null);
        }}
      />
    </Card>
  );
}

/** One media slot: a labelled pick/clear button + the current count. */
function MediaRow({
  title,
  hint,
  ids,
  onPick,
  onClear,
  clearLabel = "clear",
}: {
  title: string;
  hint: string;
  ids: number[];
  onPick: () => void;
  onClear: () => void;
  /** Text for the clear link — e.g. "clear media" when a teaser clear sits beside it. */
  clearLabel?: string;
}) {
  return (
    <div className="flex items-start justify-between gap-3 border border-border rounded-md px-3 py-2">
      <div className="min-w-0">
        <div className="text-xs font-medium text-fg">{title}</div>
        <div className="text-[11px] text-fg-dim">{hint}</div>
      </div>
      <div className="flex items-center gap-2 shrink-0">
        <Button size="sm" variant="secondary" onClick={onPick}>
          {ids.length ? `🖼 ${ids.length} picked` : "🖼 Pick media"}
        </Button>
        {ids.length > 0 && (
          <button
            type="button"
            onClick={onClear}
            className="text-[11px] text-fg-dim hover:text-err"
          >
            {clearLabel}
          </button>
        )}
      </div>
    </div>
  );
}

/** A horizontal strip of media thumbnails for one slot, in SEND order. Each tile
 *  shows the image (or a "#id" badge until it resolves), a 🔓/🔒 free/locked flag
 *  when `free` is given (leading `free` = the teaser), and ◀ ▶ to reorder + ✕ to
 *  drop it. Reordering matters: the first `free` images are the ones sent free. */
function MediaStrip({
  ids,
  free,
  thumbFor,
  onMove,
  onRemove,
}: {
  ids: number[];
  /** How many LEADING tiles are the free teaser (🔓); omit for opener media. */
  free?: number;
  thumbFor: (id: number) => string;
  onMove: (index: number, dir: -1 | 1) => void;
  onRemove: (index: number) => void;
}) {
  return (
    <div className="flex flex-wrap gap-2 pl-1">
      {ids.map((id, i) => {
        const src = thumbFor(id);
        const isFree = free != null && i < free;
        return (
          <div
            key={`${id}-${i}`}
            className={`relative w-16 group rounded overflow-hidden border ${
              free == null
                ? "border-border"
                : isFree
                  ? "border-emerald-500/60"
                  : "border-border"
            }`}
            title={`#${id}${free == null ? "" : isFree ? " · free teaser" : " · locked"}`}
          >
            <div className="relative h-16 w-16 bg-bg-elev-1">
              {src ? (
                // eslint-disable-next-line @next/next/no-img-element
                <img
                  src={src}
                  alt={`media ${id}`}
                  className="h-16 w-16 object-cover"
                  draggable={false}
                />
              ) : (
                <div className="h-16 w-16 flex items-center justify-center text-[10px] text-fg-dim">
                  #{id}
                </div>
              )}
              {free != null && (
                <span className="absolute top-0.5 left-0.5 text-[10px] leading-none drop-shadow">
                  {isFree ? "🔓" : "🔒"}
                </span>
              )}
              <button
                type="button"
                onClick={() => onRemove(i)}
                className="absolute top-0.5 right-0.5 h-6 w-6 md:h-4 md:w-4 rounded-full bg-black/55 text-white text-[10px] leading-none opacity-100 md:opacity-0 md:group-hover:opacity-100 flex items-center justify-center"
                aria-label={`Remove media ${id}`}
                title="Remove"
              >
                ✕
              </button>
            </div>
            {/* Reorder controls — hover-revealed on desktop, always visible (and
             *  thumb-sized) on touch, where hover never fires. */}
            <div className="flex items-stretch justify-between bg-black/40 py-2 md:py-0 opacity-100 md:opacity-0 md:group-hover:opacity-100">
              <button
                type="button"
                onClick={() => onMove(i, -1)}
                disabled={i === 0}
                className="flex-1 text-[10px] text-white/90 disabled:opacity-30 hover:bg-white/10 leading-4"
                aria-label="Move left"
                title="Move earlier"
              >
                ◀
              </button>
              <span className="text-[9px] text-white/60 leading-4 px-0.5">{i + 1}</span>
              <button
                type="button"
                onClick={() => onMove(i, 1)}
                disabled={i === ids.length - 1}
                className="flex-1 text-[10px] text-white/90 disabled:opacity-30 hover:bg-white/10 leading-4"
                aria-label="Move right"
                title="Move later"
              >
                ▶
              </button>
            </div>
          </div>
        );
      })}
    </div>
  );
}
