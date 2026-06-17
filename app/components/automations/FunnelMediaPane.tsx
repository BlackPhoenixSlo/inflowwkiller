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
import type { VaultMedia } from "@/lib/relay";
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
          <MediaRow
            title="Opening message media"
            hint="Sent with the funnel opener (optional)."
            ids={openerIds}
            onPick={() => setPickerFor("opener")}
            onClear={() => { setDone(false); setOpenerIds([]); }}
          />

          {/* PPV step media */}
          {ppvSlots.length === 0 ? (
            <div className="text-[11px] text-fg-dim italic">
              This funnel has no PPV steps yet. Add a PPV step in the funnel
              editor (and save), then attach its media here.
            </div>
          ) : (
            ppvSlots.map((slot) => {
              const d = steps[String(slot.stepNo)] ?? { ids: [], previewCount: 0 };
              return (
                <div key={slot.stepNo} className="space-y-1.5">
                  <MediaRow
                    title={slot.label}
                    hint="The paywalled vault media for this PPV step."
                    ids={d.ids}
                    onPick={() => setPickerFor(String(slot.stepNo))}
                    onClear={() => patchStep(slot.stepNo, { ids: [], previewCount: 0 })}
                  />
                  {d.ids.length > 0 && (
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
                      of {d.ids.length} shown free as a teaser
                      {d.previewCount === 0 && " (0 = fully locked)"}
                    </label>
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
}: {
  title: string;
  hint: string;
  ids: number[];
  onPick: () => void;
  onClear: () => void;
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
            clear
          </button>
        )}
      </div>
    </div>
  );
}
