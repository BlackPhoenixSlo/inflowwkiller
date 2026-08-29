"use client";

/**
 * CaptionSetPanel — the PPV Library's "write me a caption set" control.
 *
 * A set, not a caption: `ppv_send` picks ONE of a PPV's caption boxes at random
 * per send, so a set of boxes IS the rotation. Because that pick is uniform,
 * EVERY box has to be a whole message — which is why "how many carry the pool
 * line" is no longer a knob (it was 8 of 10, and the other 2 shipped with no
 * unlock ask on them; see `service/ppv_captions.py`). What is left is a control
 * because the right mix is a house decision, not a code one.
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
  const parts = [`Added ${r.captions.length} caption box(es)`];
  if (r.pool_lines) parts.push("every one carries this PPV's pool line");
  else parts.push("this style pool has no lines — bare hooks, no unlock ask");
  return parts.join(" · ");
}

export interface CaptionSetKnobs {
  /** The server's ceiling, echoed so the panel's inputs bound to it too. */
  boxesMax: number;
  boxes: number;
  frameTop: number;
  styles: string[];
  /** Clamped exactly the way the server clamps, so the readout never promises a
   *  shape the route would refuse. */
  clamped: { boxes: number; frameTop: number };
  setBoxes: (n: number) => void;
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
  // NONE on top by default. The pool lines are written as closing asks ("unlock
  // it for me"), so a line above the hook makes the ask the opener and the tease
  // the afterthought. Mirrors `ComposeSpec.frame_top`.
  const [frameTop, setFrameTop] = useState(0);
  const [styles, setStyles] = useState<string[]>(CAPTION_STYLES.map((s) => s.key));

  // Ordered, because the knobs nest: you cannot put the pool line on top of more
  // boxes than exist. Mirrors `ComposeSpec.clamped`.
  const cBoxes = Math.max(1, Math.min(boxes || 1, boxesMax));
  const cFrameTop = Math.max(0, Math.min(frameTop, cBoxes));

  return {
    boxesMax, boxes, frameTop, styles,
    clamped: { boxes: cBoxes, frameTop: cFrameTop },
    setBoxes, setFrameTop,
    toggleStyle: (key) =>
      setStyles((ks) => (ks.includes(key) ? ks.filter((k) => k !== key) : [...ks, key])),
  };
}

export function CaptionSetPanel({
  accountId, mediaIds, captionPoolKey, knobs, onAdd, aiCaptionAtSend = false,
}: {
  accountId: string | null;
  /** The PPV's media, in its order — DRAFT ids, so this works before Save. */
  mediaIds: number[];
  /** The PPV's lane. Its house lines become the frames. */
  captionPoolKey: string;
  knobs: CaptionSetKnobs;
  onAdd: (captions: string[]) => void;
  /** The account's "write a fresh caption line at every send" switch, as the
   *  editor currently has it — the UNSAVED value, because the operator can flip
   *  it and press this button without saving in between, and the send path will
   *  eventually read whatever they do save. On means `ppv_send` puts a SECOND
   *  written hook above whichever of these boxes it draws. */
  aiCaptionAtSend?: boolean;
}) {
  const boxSetM = useCaptionBoxSet(accountId);
  const [msg, setMsg] = useState("");
  const { boxes, frameTop, styles, clamped, boxesMax } = knobs;

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

      {aiCaptionAtSend && (
        <div className="rounded-lg border border-warn/50 bg-warn/10 p-2 text-[11px] leading-relaxed text-fg">
          <b>Heads up — two openers.</b> This account has{" "}
          <b>&quot;Write a fresh caption line at every send&quot;</b> on. These boxes already
          open with a written hook, and at send time a <b>second</b> one is placed above it,
          so the fan reads two openers about the same media plus the ask. Turn that switch
          off for this account, or write these boxes by hand.
        </div>
      )}

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

      <div className="grid grid-cols-2 gap-2">
        {num("Boxes", boxes, boxesMax, knobs.setBoxes)}
        {num("Line on top", frameTop, clamped.boxes, knobs.setFrameTop)}
      </div>

      <div className="text-[11px] text-fg-dim/70 leading-relaxed">
        {captionPoolKey
          ? <>All {clamped.boxes} boxes carry the style-pool line — one box is picked at
              random each send, so a box without the unlock ask is a send without one.{" "}
              {clamped.frameTop === 0
                ? "The line sits below the hook on every box."
                : `${clamped.frameTop} of them put the pool line above the hook; the rest sit below it.`}</>
          : <>This PPV has <b>no style pool</b>, so these boxes are bare hooks with no
              unlock ask under them. Pick a style pool above, or write the ask into the
              boxes yourself before you save.</>}
      </div>

      <Button size="sm" onClick={write} disabled={boxSetM.isPending}>
        {boxSetM.isPending ? "Writing…" : `Write ${clamped.boxes} caption box(es)`}
      </Button>

      {msg && <div className="text-[11px] text-fg-dim">{msg}</div>}
    </div>
  );
}
