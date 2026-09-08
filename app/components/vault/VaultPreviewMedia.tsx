"use client";

/**
 * The media block in the vault manager's details drawer, and the audio player it
 * falls back to.
 *
 * Lifted out of `VaultManagePanel` — which is a 1,600-line file whose main
 * component is a 1,000-line function — because this is the block the still
 * ladder lives in and the one being changed. It closes over nothing: it reads
 * its media and account from props and everything else from module scope, so
 * the move is a cut and paste. The other components below it in that file are
 * the same shape and a separate diff; moving them here too would widen this one
 * for no reason.
 *
 * A selected video plays INLINE with native controls (same `.vault-video`
 * styling the full-screen player uses), so watching a clip never leaves the
 * grid. The ⤢ button hands off to the picker's `VaultVideoPreview` for
 * full-screen. DRM videos have no progressive mp4 to play, so they fall back to
 * the still + a ▶ that opens VaultVideoPreview's poster-frame lightbox — the
 * same behaviour as the picker. Photos just render.
 */

import { useState } from "react";

import { isDrmOnlyVideo, progressiveVideoSrc } from "@/components/chat/VaultPicker";
import ImageLightbox from "@/components/vault/ImageLightbox";
import { useStillLadder } from "@/hooks/useStillLadder";
import { mirrorFullSrc } from "@/hooks/useVaultCache";
import { proxyImage } from "@/lib/mediaUrl";
import { type VaultMedia } from "@/lib/relay";

/** A voice note in the details drawer: the player, and nothing else.
 *
 *  Its own component because it shares NOTHING with the picture path below — no
 *  still, no lightbox, no zoom, no ⤢ — and every one of those would resolve to
 *  `files.full`, which for audio IS the audio. `preload="none"` so the file is
 *  fetched only if somebody presses play.
 *
 *  Module-private. It came out of `VaultManagePanel` with the picture path it
 *  is a sibling of, and it has exactly one caller, below — exporting it would
 *  have made the extraction widen the public surface, which is the opposite of
 *  what the extraction was for. */
function VaultAudioPreview({
  media: m,
  accountId,
}: {
  media: VaultMedia;
  accountId: string;
}) {
  const src = proxyImage(m.files?.full?.url, accountId);
  return (
    <div className="w-full rounded-lg bg-black px-3 py-4 grid gap-2 justify-items-center text-fg-dim">
      <span className="text-2xl leading-none" aria-hidden>🎤</span>
      <audio src={src || undefined} controls preload="none" className="w-full" />
    </div>
  );
}

export default function VaultPreviewMedia({
  media: m,
  accountId,
  onExpand,
}: {
  media: VaultMedia;
  accountId: string;
  onExpand: () => void;
}) {
  const isVideo = m.type === "video";
  const rawSrc = isVideo ? progressiveVideoSrc(m) : null;
  const drmOnly = isVideo && isDrmOnlyVideo(m);
  const playable = isVideo && !!rawSrc && !drmOnly;
  const idNum = Number(m.id);
  const [zoom, setZoom] = useState(false);

  // A PHOTO shows the FULL FRAME from the permanent cache — not `_thumb`, which
  // is a 300x300 centre-crop that lops the top and bottom off a 3:4 portrait and
  // hides edge-of-frame detail. A VIDEO keeps its poster (the square is fine as a
  // play affordance; ⤢ opens the real player).
  const photoFull = !isVideo && Number.isFinite(idNum) ? mirrorFullSrc(accountId, idNum) : null;
  // The rungs, best first. See `useStillLadder` for what the store can actually
  // answer with and why a fallback is worth having at all. `still` is null if
  // and only if every rung has failed — one fact, one piece of state.
  const fallbacks = [
    m._thumb,
    proxyImage(
      m.files?.full?.url || m.files?.preview?.url || m.files?.thumb?.url,
      accountId,
    ),
  ];
  const still = useStillLadder([photoFull, ...fallbacks]);

  // Hook order first: audio never reaches the ladder, but the early return has
  // to come after every hook this component runs.
  if (m.type === "audio") return <VaultAudioPreview media={m} accountId={accountId} />;

  // Both branches of the non-playable case need it, including the one that used
  // to render NOTHING: a DRM or poster-less video with an exhausted ladder had
  // neither an <img> nor a message, which is the blank black box the message was
  // added to eliminate.
  const noPreview = (
    <div className="w-full py-10 text-center text-xs text-fg-dim">
      {isVideo
        ? "No preview frame — press ▶ to play, or run Collect to cache one."
        : "No preview available — the media is in the vault; run Collect to cache its picture."}
    </div>
  );

  return (
    <div className="relative w-full rounded-lg overflow-hidden bg-black">
      {playable ? (
        <video
          src={proxyImage(rawSrc, accountId)}
          // `still.src` and not the first rung: a poster url that has already
          // 404'd is a dead attribute the browser retries on every render.
          poster={still.src || undefined}
          controls
          loop
          playsInline
          className="vault-video w-full max-h-[30vh] object-contain bg-black"
        />
      ) : (
        <>
          {still.src ? (
            // eslint-disable-next-line @next/next/no-img-element
            <img
              src={still.src}
              alt=""
              aria-hidden={isVideo}
              onError={still.onError}
              onClick={isVideo ? undefined : () => setZoom(true)}
              title={isVideo ? undefined : "Click to view full size"}
              className={`w-full max-h-[30vh] object-contain${isVideo ? "" : " cursor-zoom-in"}`}
            />
          ) : (
            // Say so. A blank black box is how a rendering failure gets read as
            // a missing upload, and then re-uploaded — onto OnlyFans' byte
            // dedupe, which returns the same id and changes nothing.
            noPreview
          )}
          {isVideo && (
            <button
              type="button"
              onClick={onExpand}
              title={drmOnly ? "DRM — show preview frames" : "Preview video"}
              aria-label="Preview video"
              className="absolute inset-0 grid place-items-center group"
            >
              <span className="w-14 h-14 rounded-full bg-black/60 border border-white/70 grid place-items-center text-white text-xl pl-1 transition-colors group-hover:bg-black/85">
                ▶
              </span>
            </button>
          )}
        </>
      )}
      <button
        type="button"
        onClick={isVideo ? onExpand : () => setZoom(true)}
        title="Full screen"
        aria-label="Full screen"
        className="absolute top-1 right-1 px-1.5 py-0.5 rounded bg-black/60 hover:bg-black/85 text-white text-xs leading-none"
      >
        ⤢
      </button>
      {zoom && Number.isFinite(idNum) && (
        /* The same rungs. The lightbox is what this component's own
           click-to-zoom opens, and it rendered `mirrorFullSrc` with no fallback
           at all — so the tile could recover from a dead store while clicking it
           gave a broken-image icon. */
        <ImageLightbox
          accountId={accountId}
          mediaId={idNum}
          fallbacks={fallbacks}
          onClose={() => setZoom(false)}
        />
      )}
    </div>
  );
}
