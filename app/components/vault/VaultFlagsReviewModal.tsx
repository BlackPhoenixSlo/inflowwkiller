"use client";

/**
 * VaultFlagsReviewModal — check what the vision model decided, item by item.
 *
 * The flags pass answers, per region of her body, whether she is bare, covered
 * or not in the picture. Those three answers decide which folder an item lands
 * in, what it may be priced at, and whether it is safe to put in front of a
 * whole list. It is a vision model guessing, and it has been wrong in a
 * DIFFERENT direction after every prompt change — first over-reporting
 * exposure (a tank top read as bare), then under-reporting it (a still of
 * fingers inside her read as "covered by a thong"). Every one of those
 * reversals was invisible to every automatic check and obvious to a human in
 * about four seconds.
 *
 * So checking is part of the product, not part of a test script.
 *
 * Each card is PRE-SET to what the model currently believes. Tap only what is
 * wrong — a pass over a hundred items should be a handful of clicks. Saving
 * records two things and both matter:
 *
 *   · a correction   → locked permanently. No re-run can revert it, so the
 *                      data is right from then on however the model behaves.
 *   · a confirmation → the answers left alone, stamped with the prompt version
 *                      that produced them. This is the half that is easy to
 *                      skip and the half that makes the accuracy number real:
 *                      without it, "looked and agreed" and "never opened" are
 *                      the same record.
 *
 * Ungraded items come first, and within them the ones that gate money or a
 * mass send — two rounds of tuning were once measured entirely on items that
 * had already been checked, and a regression sat in the unchecked remainder
 * through both without moving any total.
 *
 * No OF writes. Nothing here sends anything.
 */

import { useMemo, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";

import {
  gradeFlags,
  mirrorThumbSrc,
  useFlagsAccuracy,
  useFlagsReview,
  type FlagsReviewItem,
} from "@/hooks/useVaultCache";

const REGIONS = ["breasts_vis", "vulva_vis", "anus_vis"] as const;
const STATES = ["bare", "covered", "not_in_frame"] as const;

const LABEL: Record<string, string> = {
  breasts_vis: "breasts",
  vulva_vis: "vulva",
  anus_vis: "anus",
};
const STATE_LABEL: Record<string, string> = {
  bare: "bare",
  covered: "covered",
  not_in_frame: "not in",
};

function stateClass(state: string, on: boolean): string {
  if (!on) return "border-border bg-bg text-fg-dim hover:border-fg-dim/60";
  if (state === "bare") return "border-rose-500/70 bg-rose-500/20 text-rose-200";
  if (state === "covered") return "border-sky-500/70 bg-sky-500/20 text-sky-200";
  return "border-slate-500/70 bg-slate-500/20 text-slate-200";
}

function Card({
  accountId,
  item,
  onSaved,
}: {
  accountId: string;
  item: FlagsReviewItem;
  onSaved: (mediaId: number, corrected: number) => void;
}) {
  const [picked, setPicked] = useState<Record<string, string>>({});
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  /** What the model said, unless the operator has tapped something else. */
  const shown = (region: string) => picked[region] ?? item.regions[region] ?? "";
  const changed = Object.entries(picked).filter(
    ([r, v]) => v !== item.regions[r],
  );

  async function save() {
    setBusy(true);
    setErr(null);
    try {
      await gradeFlags(accountId, item.media_id, Object.fromEntries(changed), note);
      onSaved(item.media_id, changed.length);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  const named = Object.entries(item.over).filter(([, v]) => v);

  return (
    <div
      className={`rounded-lg border bg-bg-soft overflow-hidden flex flex-col ${
        changed.length ? "border-amber-500/60" : "border-border"
      }`}
    >
      <img
        src={mirrorThumbSrc(accountId, item.media_id)}
        alt=""
        className="w-full h-60 object-cover bg-black/40"
      />
      <div className="p-2.5 space-y-2 flex-1 flex flex-col">
        {REGIONS.map((region) => {
          const locked = item.locked.includes(region);
          return (
            <div key={region} className="flex items-center gap-1.5">
              <span className="text-[10px] text-fg-dim w-12 shrink-0">
                {locked ? "🔒 " : ""}
                {LABEL[region]}
              </span>
              <div className="flex gap-1 flex-1">
                {STATES.map((s) => (
                  <button
                    key={s}
                    type="button"
                    disabled={locked || busy}
                    onClick={() => setPicked((p) => ({ ...p, [region]: s }))}
                    className={`flex-1 text-[10px] font-semibold py-1 rounded border disabled:opacity-40 ${stateClass(
                      s,
                      shown(region) === s,
                    )}`}
                  >
                    {STATE_LABEL[s]}
                  </button>
                ))}
              </div>
            </div>
          );
        })}

        {named.length > 0 && (
          <p className="text-[10.5px] text-fg-dim leading-snug">
            {named.map(([r, v]) => (
              <span key={r} className="block">
                {LABEL[r]}: <b className="text-fg">{v}</b>
              </span>
            ))}
          </p>
        )}

        <p className="text-[10.5px] text-fg-dim line-clamp-2 leading-snug">
          {item.description}
        </p>
        <p className="text-[10px] text-fg-dim">
          #{item.media_id} · {item.kind} · {item.explicitness} · {item.clothing_state}
          {item.lanes.length > 0 && ` · ${item.lanes.join(", ")}`}
        </p>
        {item.iffy_why.length > 0 && (
          <ul className="space-y-0.5">
            {item.iffy_why.map((w) => (
              <li key={w} className="text-[10.5px] text-amber-400/90 leading-snug">
                ⚠ {w}
              </li>
            ))}
          </ul>
        )}

        <input
          value={note}
          onChange={(e) => setNote(e.target.value)}
          placeholder="anything odd? (optional)"
          className="w-full bg-bg border border-border rounded px-2 py-1 text-[11px]"
        />

        <button
          type="button"
          disabled={busy}
          onClick={save}
          className={`mt-auto rounded-md px-2 py-1.5 text-[11px] font-medium border disabled:opacity-40 ${
            changed.length
              ? "border-amber-500/60 bg-amber-500/15 text-amber-200 hover:bg-amber-500/25"
              : "border-emerald-500/50 bg-emerald-500/10 text-emerald-300 hover:bg-emerald-500/20"
          }`}
        >
          {busy
            ? "Saving…"
            : changed.length
              ? `Fix ${changed.length} and continue`
              : "Looks right"}
        </button>
        {err && <p className="text-[10.5px] text-rose-400">{err}</p>}
      </div>
    </div>
  );
}

export default function VaultFlagsReviewModal({
  accountId,
  onClose,
}: {
  accountId: string;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const [onlyIffy, setOnlyIffy] = useState(false);
  const { data, isLoading, error } = useFlagsReview(accountId, true, onlyIffy);
  const { data: acc } = useFlagsAccuracy(accountId, true);
  const [done, setDone] = useState<Map<number, number>>(new Map());

  const open = useMemo(
    () => (data?.items ?? []).filter((i) => !done.has(i.media_id)),
    [data, done],
  );
  const corrected = [...done.values()].filter((n) => n > 0).length;

  function onSaved(mediaId: number, changes: number) {
    setDone((prev) => new Map(prev).set(mediaId, changes));
    // A correction re-cuts the lanes and re-bands the prices.
    for (const key of [
      "vault-flags-accuracy",
      "vault-disputes",
      "vault-ai-folder-plan",
      "vault-mirror-items",
    ]) {
      qc.invalidateQueries({ queryKey: [key] });
    }
  }

  const pct =
    acc?.accuracy != null ? `${(acc.accuracy * 100).toFixed(1)}%` : "not enough checked yet";

  return (
    <div className="fixed inset-0 z-50 bg-black/50 grid place-items-center p-4" onClick={onClose}>
      <div
        className="w-full max-w-6xl max-h-[90vh] overflow-auto rounded-xl border border-border bg-bg shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <header className="px-4 py-3 border-b border-border sticky top-0 bg-bg z-10 flex items-start justify-between gap-3">
          <div className="min-w-0">
            <h2 className="text-sm font-semibold">Check the vision model</h2>
            <p className="text-[11px] text-fg-dim">
              Each card is already set to what the model thinks. <b>Tap only what is
              wrong</b>, then save. A fix is permanent — no re-run can undo it. Pressing
              “Looks right” is not a no-op either: it is what the accuracy below is
              measured from.
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="text-fg-dim hover:text-fg text-sm px-2 shrink-0"
          >
            ✕
          </button>
        </header>

        <div className="px-4 py-2 border-b border-border flex flex-wrap gap-4 text-[11px] text-fg-dim">
          <span>
            model agrees with you: <b className="text-fg">{pct}</b>
            {acc?.answers ? ` (${acc.answers} answers)` : ""}
          </span>
          <span>
            checked: <b className="text-fg">{(acc?.graded_items ?? 0) + done.size}</b> /{" "}
            {data?.total ?? "…"}
          </span>
          <span>prompt v{data?.prompt_version ?? "?"}</span>
          <button
            type="button"
            onClick={() => setOnlyIffy((v) => !v)}
            className="underline underline-offset-2 hover:text-fg"
            title="Measured on the graded vault: the badges flag 40 items to catch 11 of the 24 actually wrong — 28% precision, 46% recall. Good enough to sort by, NOT good enough to hide behind, which is why the full list is the default."
          >
            {onlyIffy
              ? `only the ${data?.iffy ?? "…"} flagged — show all ${data?.total ?? ""}`
              : `all ${data?.total ?? ""}, flagged first (${data?.iffy ?? "…"} flagged) — narrow`}
          </button>
          {corrected > 0 && <span className="text-amber-300">{corrected} fixed just now</span>}
          {!!acc?.graded_other_versions && (
            <span title="Graded against an older prompt — not counted, because a score across two prompts measures neither.">
              {acc.graded_other_versions} checked on an older prompt
            </span>
          )}
        </div>

        <div className="p-4">
          {isLoading && <p className="text-[12px] text-fg-dim">Loading…</p>}
          {error && (
            <p className="text-[12px] text-rose-400">
              {error instanceof Error ? error.message : String(error)}
            </p>
          )}
          {data && open.length === 0 && (
            <p className="text-[12px] text-emerald-400">
              {onlyIffy
                ? "Nothing left worth checking. Switch to “show all” for a full pass."
                : "Nothing left in this batch — close and reopen for the next one."}
            </p>
          )}
          <div className="grid grid-cols-[repeat(auto-fill,minmax(240px,1fr))] gap-3">
            {open.map((it) => (
              <Card key={it.media_id} accountId={accountId} item={it} onSaved={onSaved} />
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
