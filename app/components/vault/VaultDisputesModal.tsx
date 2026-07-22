"use client";

/**
 * VaultDisputesModal — settle a disagreement between the two vision passes.
 *
 * Two passes look at the same picture. `describe` writes prose plus
 * `clothing_state`; the cheap `flags` pass writes, per region, whether she is
 * bare / covered / not in frame, and NAMES what is covering her. Where they
 * contradict, one of them is provably wrong — which is free ground truth, no
 * hand labels needed.
 *
 * WHICH one is wrong differs item by item, so this asks rather than decides.
 * On the graded vault every current dispute is `clothing_state: fully_nude` on a still
 * where the flags name a thong, and the flags are right; the mirror-image case
 * is equally real, and guessing it wrong is what sells a fan something that is
 * not in the media.
 *
 * A choice is written into the AI fields (so the folder lanes, the price tiers
 * and the copy brief all see it with no change to any of them), recorded as an
 * operator override, and LOCKED — a forced re-flag refreshes everything except
 * the corrected field. A correction a re-run silently reverts is worse than no
 * correction, because the operator would never think to look again.
 *
 * No OF writes. Nothing here sends anything.
 */

import { useMemo, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";

import {
  mirrorFullSrc,
  mirrorPosterSrc,
  resolveDispute,
  useVaultDisputes,
  type VaultDispute,
} from "@/hooks/useVaultCache";
import ImageLightbox from "@/components/vault/ImageLightbox";

const REGION_LABEL: Record<string, string> = {
  vulva_vis: "vulva",
  breasts_vis: "breasts",
  anus_vis: "anus",
};

const STATE_STYLE: Record<string, string> = {
  bare: "border-rose-500/50 bg-rose-500/10 text-rose-300",
  covered: "border-sky-500/50 bg-sky-500/10 text-sky-300",
  not_in_frame: "border-slate-600/50 bg-slate-600/10 text-slate-400",
};

/** Render a proposed write the way it will actually land, so nothing about the
 *  choice is hidden behind a friendly label. */
function ProposalLines({ values }: { values: Record<string, unknown> }) {
  const entries = Object.entries(values);
  if (!entries.length) return <p className="text-[11px] text-fg-dim">no change</p>;
  return (
    <ul className="space-y-0.5">
      {entries.map(([k, v]) => (
        <li key={k} className="text-[11px] font-mono text-fg-dim">
          {REGION_LABEL[k] ?? k}
          {" → "}
          <span className="text-fg">
            {v === null ? "—" : Array.isArray(v) ? (v.length ? v.join(", ") : "none") : String(v)}
          </span>
        </li>
      ))}
    </ul>
  );
}

/** A photo shows one full frame. A video CANNOT be judged from one still — the
 *  two vision passes each sample different frames of it, which is half of why
 *  they "disagree" — so a video is marked and hover-scrubs its poster frames:
 *  move the cursor left→right across it to run frame 1→N. Reverts to frame 1 on
 *  leave. Clicking still opens the lightbox. */
function CardMedia({
  accountId,
  d,
  onZoom,
}: {
  accountId: string;
  d: VaultDispute;
  onZoom: () => void;
}) {
  const isVideo = d.kind === "video" && d.frame_count >= 2;
  const [frame, setFrame] = useState(0);
  const ref = useRef<HTMLDivElement>(null);

  function scrub(e: React.MouseEvent) {
    if (!isVideo || !ref.current) return;
    const r = ref.current.getBoundingClientRect();
    const f = Math.floor(((e.clientX - r.left) / r.width) * d.frame_count);
    setFrame(Math.min(d.frame_count - 1, Math.max(0, f)));
  }

  const src = isVideo
    ? mirrorPosterSrc(accountId, d.media_id, frame)
    : mirrorFullSrc(accountId, d.media_id);

  return (
    <div
      ref={ref}
      onClick={onZoom}
      onMouseMove={scrub}
      onMouseLeave={() => setFrame(0)}
      title={isVideo ? "Drag across to scrub · click to enlarge" : "Click to view full size"}
      className="relative w-full h-56 bg-black/70 cursor-zoom-in overflow-hidden"
    >
      {/* Full FRAME, letterboxed — never the square crop. The whole point of a
          correction is to judge what is on show, and the square hides the top
          and bottom of a 3:4 portrait. */}
      <img src={src} alt="" className="w-full h-full object-contain" draggable={false} />
      {isVideo && (
        <>
          <span className="absolute top-1.5 left-1.5 rounded bg-black/70 px-1.5 py-px text-[10px] font-semibold tracking-wide text-white">
            ▶ VIDEO
          </span>
          <span className="absolute bottom-2 right-1.5 rounded bg-black/70 px-1.5 py-px font-mono text-[10px] text-white/90">
            {frame + 1}/{d.frame_count}
          </span>
          <div className="absolute inset-x-0 bottom-0 h-1 bg-white/20">
            <div
              className="h-full bg-sky-400/80"
              style={{ width: `${((frame + 1) / d.frame_count) * 100}%` }}
            />
          </div>
        </>
      )}
    </div>
  );
}

function DisputeCard({
  accountId,
  d,
  onDone,
}: {
  accountId: string;
  d: VaultDispute;
  onDone: () => void;
}) {
  const [busy, setBusy] = useState<"flags" | "describe" | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [zoom, setZoom] = useState(false);

  async function pick(side: "flags" | "describe") {
    setBusy(side);
    setErr(null);
    try {
      await resolveDispute(accountId, d.media_id, d.propose[side]);
      onDone();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  const flagsSide = d.propose.flags;
  const descSide = d.propose.describe;

  return (
    <div className="rounded-lg border border-border bg-bg-soft overflow-hidden flex flex-col">
      <CardMedia accountId={accountId} d={d} onZoom={() => setZoom(true)} />
      {zoom && (
        <ImageLightbox accountId={accountId} mediaId={d.media_id} onClose={() => setZoom(false)} />
      )}
      <div className="p-3 space-y-2 flex-1 flex flex-col">
        <div className="flex flex-wrap gap-1">
          {Object.entries(d.regions).map(([r, state]) => (
            <span
              key={r}
              className={`px-1.5 py-px rounded-full border text-[10px] font-medium ${
                STATE_STYLE[state] ?? STATE_STYLE.not_in_frame
              }`}
            >
              {REGION_LABEL[r] ?? r}: {state}
            </span>
          ))}
        </div>

        {Object.entries(d.over).some(([, v]) => v) && (
          <p className="text-[11px] text-fg-dim leading-snug">
            {Object.entries(d.over)
              .filter(([, v]) => v)
              .map(([r, v]) => (
                <span key={r} className="block">
                  {REGION_LABEL[r] ?? r}: <b className="text-fg">{v}</b>
                </span>
              ))}
          </p>
        )}

        <p className="text-[11px] text-fg-dim line-clamp-3 leading-snug">{d.description}</p>
        <p className="text-[10px] text-fg-dim">
          #{d.media_id} · {d.kind} · {d.explicitness} · {d.clothing_state}
        </p>

        {d.reasons.map((r) => (
          <p key={r} className="text-[11px] text-rose-400 leading-snug">
            ⚠ {r}
          </p>
        ))}

        <div className="mt-auto pt-2 grid grid-cols-2 gap-2">
          <button
            type="button"
            disabled={!!busy}
            onClick={() => pick("flags")}
            className="rounded-md border border-sky-500/60 bg-sky-500/10 px-2 py-1.5 text-left hover:bg-sky-500/20 disabled:opacity-40"
          >
            <span className="block text-[11px] font-semibold text-sky-300">
              {busy === "flags" ? "Saving…" : "Flags were right"}
            </span>
            <ProposalLines values={flagsSide} />
          </button>
          <button
            type="button"
            disabled={!!busy}
            onClick={() => pick("describe")}
            className="rounded-md border border-amber-500/60 bg-amber-500/10 px-2 py-1.5 text-left hover:bg-amber-500/20 disabled:opacity-40"
          >
            <span className="block text-[11px] font-semibold text-amber-300">
              {busy === "describe" ? "Saving…" : "Describe was right"}
            </span>
            <ProposalLines values={descSide} />
          </button>
        </div>
        {err && <p className="text-[11px] text-rose-400">{err}</p>}
      </div>
    </div>
  );
}

export default function VaultDisputesModal({
  accountId,
  onClose,
}: {
  accountId: string;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const { data, isLoading, error } = useVaultDisputes(accountId, true);
  const [done, setDone] = useState<Set<number>>(new Set());

  const open = useMemo(
    () => (data?.disputes ?? []).filter((d) => !done.has(d.media_id)),
    [data, done],
  );

  /** Drop the card locally and invalidate everything the fix can move: the
   *  lanes re-cut, the price tiers re-band, and the mirror rows re-render. */
  function markDone(mediaId: number) {
    setDone((prev) => new Set(prev).add(mediaId));
    for (const key of [
      "vault-disputes",
      "vault-ai-folder-plan",
      "vault-mirror-items",
      "vault-internal-folders",
    ]) {
      qc.invalidateQueries({ queryKey: [key] });
    }
  }

  return (
    <div className="fixed inset-0 z-50 bg-black/50 grid place-items-center p-4" onClick={onClose}>
      <div
        className="w-full max-w-6xl max-h-[88vh] overflow-auto rounded-xl border border-border bg-bg shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <header className="px-4 py-3 border-b border-border flex items-start justify-between gap-3 sticky top-0 bg-bg z-10">
          <div className="min-w-0">
            <h2 className="text-sm font-semibold">Fix disagreements</h2>
            <p className="text-[11px] text-fg-dim">
              The two vision passes contradict each other here, so at least one is wrong —
              and it is not always the same one. Pick the reading that matches what you
              see. A video shows one still by default: hover and drag across it to scrub
              the whole clip, since the passes sample different frames. Your choice is
              locked — re-running the flags refreshes everything except what you fixed.
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

        <div className="p-4">
          {isLoading && <p className="text-[12px] text-fg-dim">Checking…</p>}
          {error && (
            <p className="text-[12px] text-rose-400">
              {error instanceof Error ? error.message : String(error)}
            </p>
          )}
          {data && (
            <p className="text-[11px] text-fg-dim mb-3">
              {open.length} of {data.count} left · {data.checked} items checked
              {done.size > 0 && ` · ${done.size} fixed this session`}
            </p>
          )}
          {data && open.length === 0 && (
            <p className="text-[12px] text-emerald-400">
              Nothing left — the two passes agree on every checked item.
            </p>
          )}
          <div className="grid grid-cols-[repeat(auto-fill,minmax(260px,1fr))] gap-3">
            {open.map((d) => (
              <DisputeCard
                key={d.media_id}
                accountId={accountId}
                d={d}
                onDone={() => markDone(d.media_id)}
              />
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
