"use client";

/**
 * ImageLightbox — full-screen view of ONE vault image, for judging exposure.
 *
 * Always shows the aspect-preserving full frame (`mirrorFullSrc`), never the
 * 300x300 square: the crop is precisely where a waistband or genitalia at the
 * edge of a 3:4 portrait disappears, so a correction made against the square is
 * a correction made against less of the picture than the model that set the
 * flag saw. Click anywhere (or Esc) to close.
 */

import { useEffect } from "react";

import { mirrorFullSrc } from "@/hooks/useVaultCache";

export default function ImageLightbox({
  accountId,
  mediaId,
  onClose,
}: {
  accountId: string;
  mediaId: number;
  onClose: () => void;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div
      className="fixed inset-0 z-[60] bg-black/85 grid place-items-center p-4 cursor-zoom-out"
      onClick={onClose}
    >
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img
        src={mirrorFullSrc(accountId, mediaId)}
        alt=""
        className="max-w-full max-h-full object-contain rounded shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      />
      <button
        type="button"
        onClick={onClose}
        className="absolute top-3 right-3 size-9 grid place-items-center rounded-full bg-black/60 text-white text-lg hover:bg-black/80"
        aria-label="Close"
      >
        ×
      </button>
    </div>
  );
}
