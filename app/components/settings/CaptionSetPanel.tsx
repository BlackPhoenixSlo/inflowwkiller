"use client";

/**
 * CaptionSetPanel — the PPV Library's "write me a caption set" control.
 *
 * A set, not a caption: `ppv_send` picks ONE of a PPV's caption boxes at random
 * per send, so a set of boxes IS the rotation, and "8 framed boxes of 10" IS an
 * 80% frame rate at send time. Every knob here is a control rather than a
 * constant because the right mix is a house decision, not a code one.
 *
 * Suggest-only: the panel hands finished boxes to `onAdd` and the operator still
 * presses Save.
 */

import { useState } from "react";

import { Button } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";
import {
  useCaptionBoxSet,
  type CaptionBoxSetResult,
} from "@/hooks/usePpvLibraryConfig";

/** Matches the tab's own inputs. Kept local so the panel carries no import
 *  cycle back to the component that renders it. */
const INPUT =
  "bg-bg border border-border rounded-lg px-3 py-2 text-sm focus:outline-none focus:border-accent";

/** The hook styles, in the order the server cycles them. All four write from the
 *  same vision description and carry the same register examples — only length
 *  and shape differ. */
const CAPTION_STYLES: { key: string; label: string; hint: string }[] = [
  { key: "detail", label: "Detail", hint: "opens on the most specific thing in the media" },
  { key: "house", label: "House", hint: "pure style-pool voice" },
  { key: "blunt", label: "Blunt", hint: "under 70 characters, no emoji" },
  { key: "question", label: "Question", hint: "ends on something he wants answered" },
];

/** Why a style produced nothing, in the operator's words. The route reports
 *  reasons rather than silently returning fewer boxes — "3 skipped" with no
 *  cause sends someone hunting a phantom. */
const SKIP_REASONS: Record<string, string> = {
  no_description: "not described yet",
  missing: "not in the vault mirror",
  no_pool_lines: "this style pool has no lines",
  capped: "daily AI cap reached",
  error: "the model call failed",
  empty: "the model returned nothing",
};

function summarise(r: CaptionBoxSetResult): string {
  if (!r.captions.length) {
    const why = r.skipped[0]?.reason;
    return why ? `Nothing to add — ${SKIP_REASONS[why] ?? why}` : "Nothing to add";
  }
  const framed = r.boxes.filter((b) => b.frame).length;
  const pct = Math.round((framed / r.boxes.length) * 100);
  const parts = [
    `Added ${r.captions.length} caption box(es)`,
    `${framed} carry the pool line (${pct}% of sends)`,
  ];
  if (!r.pool_lines) parts.push("this style pool has no lines — hooks only");
  return parts.join(" · ");
}

export interface CaptionSetKnobs {
  /** The server's ceiling, echoed so the panel's inputs bound to it too. */
  boxesMax: number;
  boxes: number;
  framed: number;
  frameTop: number;
  styles: string[];
  /** Clamped exactly the way the server clamps, so the readout never promises a
   *  shape the route would refuse. */
  clamped: { boxes: number; framed: number; frameTop: number; pct: number };
  setBoxes: (n: number) => void;
  setFramed: (n: number) => void;
  setFrameTop: (n: number) => void;
  toggleStyle: (key: string) => void;
}

/** Knob state, owned by the TAB rather than the panel so an operator who tunes
 *  the mix once keeps it while moving between PPVs.
 *
 *  `boxesMax` comes from the config endpoint (`caption_limits.boxes_max`) so the
 *  "% of sends" readout is computed against the same bound the route clamps to.
 *  The literal is a pre-load fallback, not a second source of truth. */
export function useCaptionSetKnobs(boxesMax = 20): CaptionSetKnobs {
  const [boxes, setBoxes] = useState(10);
  const [framed, setFramed] = useState(8);
  const [frameTop, setFrameTop] = useState(2);
  const [styles, setStyles] = useState<string[]>(CAPTION_STYLES.map((s) => s.key));

  // Ordered, because the knobs nest: you cannot frame more boxes than exist, nor
  // put more frames on top than are framed. Mirrors `ComposeSpec.clamped`.
  const cBoxes = Math.max(1, Math.min(boxes || 1, boxesMax));
  const cFramed = Math.max(0, Math.min(framed, cBoxes));
  const cFrameTop = Math.max(0, Math.min(frameTop, cFramed));

  return {
    boxesMax, boxes, framed, frameTop, styles,
    clamped: {
      boxes: cBoxes, framed: cFramed, frameTop: cFrameTop,
      pct: Math.round((cFramed / cBoxes) * 100),
    },
    setBoxes, setFramed, setFrameTop,
    toggleStyle: (key) =>
      setStyles((ks) => (ks.includes(key) ? ks.filter((k) => k !== key) : [...ks, key])),
  };
}

export function CaptionSetPanel({
  accountId, mediaIds, captionPoolKey, knobs, onAdd,
}: {
  accountId: string | null;
  /** The PPV's media, in its order — DRAFT ids, so this works before Save. */
  mediaIds: number[];
  /** The PPV's lane. Its house lines become the frames. */
  captionPoolKey: string;
  knobs: CaptionSetKnobs;
  onAdd: (captions: string[]) => void;
}) {
  const boxSetM = useCaptionBoxSet(accountId);
  const [msg, setMsg] = useState("");
  const { boxes, framed, frameTop, styles, clamped, boxesMax } = knobs;

  const write = async () => {
    if (!mediaIds.length) {
      setMsg("Pick this PPV's media first — the hooks are written from what's in it.");
      return;
    }
    if (!styles.length) {
      setMsg("Pick at least one hook style.");
      return;
    }
    setMsg("");
    try {
      const res = await boxSetM.mutateAsync({
        mediaIds,
        captionPoolKey,
        boxes: clamped.boxes,
        framed: clamped.framed,
        frameTop: clamped.frameTop,
        styles,
      });
      if (res.captions.length) onAdd(res.captions);
      setMsg(summarise(res));
    } catch (e) {
      setMsg(e instanceof Error ? e.message : "Could not reach the vault.");
    }
  };

  const num = (label: string, value: number, max: number, set: (n: number) => void) => (
    <label className="space-y-1">
      <span className="block text-[11px] uppercase tracking-wide text-fg-dim/70">{label}</span>
      <input
        type="number" min={0} max={max} value={value}
        onChange={(e) => set(Number(e.target.value))}
        className={`${INPUT} w-full`}
      />
    </label>
  );

  return (
    <div className="space-y-2.5 rounded-lg border border-border p-3">
      <div className="text-xs text-fg-dim">
        Writes one hook per style from this PPV&apos;s media, then mixes each with a line
        from its <b>{captionPoolKey || "— no"}</b> style pool.
      </div>

      <div className="space-y-1">
        <div className="text-[11px] uppercase tracking-wide text-fg-dim/70">Hook styles</div>
        <div className="flex flex-wrap gap-1.5">
          {CAPTION_STYLES.map((st) => (
            <button
              key={st.key}
              type="button"
              title={st.hint}
              aria-pressed={styles.includes(st.key)}
              onClick={() => knobs.toggleStyle(st.key)}
              className={cn(
                "px-2.5 py-1 text-xs rounded-lg border transition-colors",
                "focus:outline-none focus:border-accent",
                styles.includes(st.key)
                  ? "border-accent text-accent"
                  : "border-border text-fg-dim hover:text-fg",
              )}
            >
              {st.label}
            </button>
          ))}
        </div>
      </div>

      <div className="grid grid-cols-3 gap-2">
        {num("Boxes", boxes, boxesMax, knobs.setBoxes)}
        {num("With pool line", framed, clamped.boxes, knobs.setFramed)}
        {num("Line on top", frameTop, clamped.framed, knobs.setFrameTop)}
      </div>

      <div className="text-[11px] text-fg-dim/70 leading-relaxed">
        {clamped.framed} of {clamped.boxes} boxes carry the style-pool line. One box is
        picked at random each send, so that is <b>{clamped.pct}% of sends</b>.{" "}
        {clamped.frameTop} of them put the pool line above the hook; the rest sit below it.
      </div>

      <Button size="sm" onClick={write} disabled={boxSetM.isPending}>
        {boxSetM.isPending ? "Writing…" : `Write ${clamped.boxes} caption box(es)`}
      </Button>

      {msg && <div className="text-[11px] text-fg-dim">{msg}</div>}
    </div>
  );
}
