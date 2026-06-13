"use client";

/**
 * FanAvatar — round fan thumbnail shared by the /messages row components
 * (MessageRow, MessageRowGeneric, TipRow). Falls back to the first letter
 * of `alt` when there's no avatar URL.
 */

import { proxyImage } from "@/lib/relay";
import { cn } from "@/lib/utils";

export default function FanAvatar({
  url,
  accountId,
  alt,
}: {
  url: string | null;
  accountId: string;
  alt: string;
}) {
  const src = url ? proxyImage(url, accountId) : "";
  return (
    <div
      className={cn(
        "w-10 h-10 rounded-full bg-bg-elev-1 border border-border overflow-hidden flex-shrink-0",
      )}
    >
      {src ? (
        // eslint-disable-next-line @next/next/no-img-element
        <img src={src} alt={alt} className="w-full h-full object-cover" />
      ) : (
        <div className="w-full h-full grid place-items-center text-fg-dim text-xs">
          {alt[0]?.toUpperCase() ?? "?"}
        </div>
      )}
    </div>
  );
}
