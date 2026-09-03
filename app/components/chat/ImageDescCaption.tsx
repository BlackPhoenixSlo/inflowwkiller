"use client";

/**
 * ImageDescCaption — the 👁 line under an incoming photo / gif bubble, showing
 * what the AI sees in it (the text the chat engines splice into their history as
 * "[he sent: …]").
 *
 * Rendering it makes the model's input auditable: a wrong read ("shirtless in a
 * car" for a dick pic) is visible in the thread instead of quietly steering the
 * reply.
 *
 * Cost discipline. HOVER is free — the tooltip shows the cached text in full —
 * and clicking the line only expands the clamp. Spending happens on exactly one
 * gesture: the explicit ↻ / "read photo" button (one vision call, ~$0.0004; a
 * Giphy gif is free either way). So scrolling a thread full of pictures can never
 * rack up describes.
 *
 * Owns its own mutation rather than taking `onDescribe`/`busy` props: which
 * single bubble is mid-describe is local UI state, and hoisting it to ChatSurface
 * meant reading `mutation.variables` back out to reconstruct the row's identity.
 */

import { useState } from "react";

import { useDescribeImage } from "@/hooks/useChatImageDesc";
import { hasDescribableStill } from "@/lib/ofMedia";
import { cn } from "@/lib/utils";
import type { OFMedia } from "@/lib/relay";
import type { FanId } from "@/lib/fanId";

export function ImageDescCaption({ accountId, fanId, messageId, desc, isGif, media }: {
  accountId: string | null;
  fanId: FanId | null;
  messageId: number;
  /** The cached read; null = this one was never described. */
  desc: string | null;
  /** A Giphy gif rather than a photo — changes the wording, and the read is free. */
  isGif?: boolean;
  /** This bubble's media, to decide whether a read is even possible. */
  media?: OFMedia[];
}) {
  const [expanded, setExpanded] = useState(false);
  const describe = useDescribeImage(accountId, fanId);
  const canDescribe = !!accountId && fanId != null
    && Number.isFinite(messageId) && messageId > 0;
  const busy = describe.isPending;
  const noun = isGif ? "gif" : "photo";
  // A 200 carrying image_desc:null means the describe genuinely found nothing to
  // read (locked media, cap hit, model refused). Saying so beats a button that
  // spins and then silently resets — the chatter would just click it again.
  const cameBackEmpty = (describe.isSuccess && !describe.data?.image_desc)
    || describe.isError;
  // …but the failure must not be terminal. Every cause here is transient or
  // fixable — the daily budget resets, a cap clears, a signature refetch may work
  // on the next try — so `reset()` puts the button back rather than leaving the
  // bubble permanently inert until something remounts it.
  const retry = () => describe.reset();

  // Never described (arrived before the feature, flag off, or the LLM cap
  // swallowed it): offer the read rather than render an empty row.
  if (!desc) {
    // …but only where a read could actually succeed. A locked clip or a voice
    // note has no frame at all, so the button could never do anything.
    if (!canDescribe || !(isGif || hasDescribableStill(media))) return null;
    if (cameBackEmpty) {
      return (
        <div className="mt-1 flex items-center gap-1.5 text-[10px] leading-tight">
          <span
            className="text-fg-dim/60 italic"
            title="Nothing readable in this one — it may be locked, or today's read budget for this fan is used up"
          >
            👁 couldn’t read that one
          </span>
          <button
            type="button"
            onClick={retry}
            className="text-fg-dim/50 hover:text-fg-dim underline decoration-dotted
                       underline-offset-2 transition-colors"
          >
            try again
          </button>
        </div>
      );
    }
    return (
      <button
        type="button"
        onClick={() => describe.mutate({ messageId, force: false })}
        disabled={busy}
        className={cn(
          "mt-1 text-[10px] leading-tight text-fg-dim/70 hover:text-fg-dim",
          "underline decoration-dotted underline-offset-2 transition-colors",
          busy && "opacity-60 cursor-wait",
        )}
        title={isGif
          ? "Look up what this gif is (free) so the AI can react to it"
          : "Run the vision model on this photo (~$0.0004) so the AI can react to it"}
      >
        {busy ? `👁 reading…` : `👁 read ${noun}`}
      </button>
    );
  }

  return (
    <div className="mt-1 flex items-start gap-1 group/desc">
      <span
        onClick={() => setExpanded((v) => !v)}
        title={desc}
        className={cn(
          "text-[10px] leading-snug text-fg-dim/80 italic cursor-pointer",
          !expanded && "line-clamp-2",
        )}
      >
        👁 {desc}
      </span>
      {canDescribe && (
        <button
          type="button"
          onClick={() => describe.mutate({ messageId, force: true })}
          disabled={busy}
          className={cn(
            "shrink-0 text-[10px] leading-none text-fg-dim/50 hover:text-fg-dim",
            "opacity-0 group-hover/desc:opacity-100 transition-opacity",
            busy && "opacity-100 cursor-wait",
          )}
          title={`Re-read this ${noun}`}
          aria-label={`Re-read this ${noun}`}
        >
          {busy ? "…" : "↻"}
        </button>
      )}
    </div>
  );
}
