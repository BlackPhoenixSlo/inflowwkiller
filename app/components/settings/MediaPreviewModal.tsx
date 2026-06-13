"use client";

/**
 * MediaPreviewModal — full-screen lightbox for one vault item, reusing the
 * exact source-resolution rules the VaultPicker preview uses:
 *
 *   • photo / gif → proxied still.
 *   • video       → progressive mp4 with native controls.
 *   • DRM-only video → OF's pre-extracted poster frames as a steppable,
 *     auto-advancing slideshow (the encrypted segments can't play here).
 *
 * Built from VaultPicker's exported helpers so the two stay in lockstep
 * without dragging the whole picker component into this screen's hot path.
 */

import { useEffect, useState } from "react";

import { proxyImage, type VaultMedia } from "@/lib/relay";
import {
  isDrmOnlyVideo, progressiveVideoSrc, videoPosterFrames,
} from "@/components/chat/VaultPicker";

function photoSrc(m: VaultMedia): string | null {
  return m.files?.full?.url
    || m.files?.preview?.url
    || m.files?.squarePreview?.url
    || m.files?.thumb?.url
    || null;
}

export function MediaPreviewModal({ media, accountId, onClose }: {
  media: VaultMedia;
  accountId: string | null;
  onClose: () => void;
}) {
  const isVideo = media.type === "video";
  const drmOnly = isDrmOnlyVideo(media);
  const drmFrames = videoPosterFrames(media);
  const drm = drmOnly && drmFrames.length > 0;
  const rawSrc = isVideo ? progressiveVideoSrc(media) : photoSrc(media);
  const src = proxyImage(rawSrc, accountId);

  const [loadError, setLoadError] = useState(false);
  const [frameIdx, setFrameIdx] = useState(0);
  useEffect(() => { setLoadError(false); setFrameIdx(0); }, [media.id]);

  // Esc closes; auto-advance the DRM frame slideshow.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  useEffect(() => {
    if (!drm || drmFrames.length < 2) return;
    const id = window.setInterval(
      () => setFrameIdx((n) => (n + 1) % drmFrames.length), 900);
    return () => window.clearInterval(id);
  }, [drm, drmFrames.length]);

  const showError = !src || loadError;

  return (
    <div className="fixed inset-0 z-[60] flex items-center justify-center">
      <button type="button" aria-label="Close preview" onClick={onClose}
        className="absolute inset-0 bg-black/75" />
      <div className="relative max-w-[90%] max-h-[90%] flex flex-col items-center gap-3">
        {drm ? (
          <div className="relative">
            <img src={proxyImage(drmFrames[frameIdx], accountId)}
              alt={`Frame ${frameIdx + 1} of ${drmFrames.length}`}
              className="max-w-full max-h-[80vh] rounded-md shadow-2xl bg-black object-contain" />
            {drmFrames.length > 1 && (
              <>
                <button type="button" aria-label="Previous frame"
                  onClick={() => setFrameIdx((n) => (n - 1 + drmFrames.length) % drmFrames.length)}
                  className="absolute left-1 top-1/2 -translate-y-1/2 w-8 h-8 rounded-full bg-black/55 hover:bg-black/80 text-white grid place-items-center">‹</button>
                <button type="button" aria-label="Next frame"
                  onClick={() => setFrameIdx((n) => (n + 1) % drmFrames.length)}
                  className="absolute right-1 top-1/2 -translate-y-1/2 w-8 h-8 rounded-full bg-black/55 hover:bg-black/80 text-white grid place-items-center">›</button>
              </>
            )}
            <span className="absolute top-2 left-2 bg-black/65 text-white text-[11px] px-2 py-0.5 rounded-full">
              🔒 DRM · frame {frameIdx + 1}/{drmFrames.length}
            </span>
          </div>
        ) : showError ? (
          <div className="px-6 py-12 bg-panel rounded-md text-sm text-fg-dim border border-border max-w-md text-center space-y-2">
            <div>{isVideo ? "Couldn't load this video." : "Couldn't load this image."}</div>
            <div className="text-[11px] opacity-70">
              {rawSrc
                ? "OF returned a URL but the upstream rejected it — usually a signed URL that expired in cache. Refresh the vault to pull fresh URLs."
                : `OF didn't return a source for vault id ${media.id}. Freshly-uploaded media sits without one until OF finishes processing (a few minutes).`}
            </div>
          </div>
        ) : isVideo ? (
          <video src={src} autoPlay controls loop playsInline
            poster={proxyImage(media.files?.preview?.url || media.files?.thumb?.url || null, accountId) || undefined}
            onError={() => setLoadError(true)}
            className="max-w-full max-h-[80vh] rounded-md shadow-2xl bg-black" />
        ) : (
          <img src={src} alt={`vault ${media.id}`}
            onError={() => setLoadError(true)}
            className="max-w-full max-h-[80vh] rounded-md shadow-2xl bg-black object-contain" />
        )}
        <button type="button" onClick={onClose}
          className="text-xs text-white/80 hover:text-white px-3 py-1 rounded-md bg-black/40 border border-white/15">
          Close (Esc)
        </button>
      </div>
    </div>
  );
}
