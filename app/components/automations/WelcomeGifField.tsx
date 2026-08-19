"use client";

/**
 * WelcomeGifField — bubble 4 of the welcome burst: one GIF, sent on its own.
 *
 * Owns the picker's open/closed state so BrainPanel doesn't have to. The value
 * itself is a bare giphy id (`""` = no GIF): that is what the rule's payload
 * stores, so an operator reading the raw JSON sees one string rather than a blob
 * of CDN metadata, and the thumbnail is derived from it via `giphyUrl`.
 */

import { useState } from "react";

import { Button } from "@/components/ui/primitives";
import { GifPickerStrip, type PickedGif } from "@/components/chat/GifPicker";
import { giphyUrl } from "@/lib/ofMedia";

export function WelcomeGifField({
  accountId, gifId, onChange,
}: {
  accountId: string | null;
  /** Bare giphy id; "" = no 4th bubble. */
  gifId: string;
  onChange: (gifId: string) => void;
}) {
  const [pickerOpen, setPickerOpen] = useState(false);

  function pick(id: string) {
    onChange(id);
    setPickerOpen(false);
  }

  return (
    <div className="flex flex-col gap-1">
      <span
        className="text-[11px] text-fg-dim"
        title="Sent as a 4th bubble after the question — the GIF on its own, no caption. Leave empty for the 3-bubble welcome."
      >
        GIF (4th bubble — sent on its own)
      </span>
      <div className="flex flex-wrap items-center gap-2">
        {gifId ? (
          <span className="flex items-center gap-1 rounded-md border border-border bg-bg-elev-2 p-1">
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img
              src={giphyUrl(gifId)}
              alt="the GIF this welcome ends on"
              className="h-16 w-auto rounded"
            />
            <Button
              size="sm"
              variant="ghost"
              title="Remove it — back to the 3-bubble welcome"
              onClick={() => pick("")}
            >
              ✕
            </Button>
          </span>
        ) : (
          <span className="text-[11px] text-fg-dim">
            None — the welcome ends on your question.
          </span>
        )}
        <Button
          size="sm"
          variant="secondary"
          disabled={!accountId}
          onClick={() => setPickerOpen((o) => !o)}
        >
          {pickerOpen ? "Close" : gifId ? "🎬 Change GIF" : "🎬 Pick a GIF"}
        </Button>
      </div>
      {pickerOpen && (
        // The strip proxies Giphy through OF itself, so it needs a LIVE session on
        // this account — on a dead one it renders its own error rather than GIFs.
        <GifPickerStrip
          accountId={accountId}
          onPick={(g: PickedGif) => pick(g.id)}
          onClose={() => setPickerOpen(false)}
        />
      )}
      <p className="text-[10px] text-fg-dim">
        The SAME GIF goes to every new subscriber — it is a fixed pick, not a
        reroll. Sent last, after a short pause, with no typing indicator.
      </p>
    </div>
  );
}
