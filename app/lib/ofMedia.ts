/**
 * ofMedia — the browser's copy of `service/of_shapes.py`.
 *
 * A DELIBERATE port, not an approximation. The rules here decide whether the chat
 * pane offers a 👁 "read this" button, and the server decides whether it can
 * actually resolve a frame; when those two answers disagree the chatter gets a
 * button that posts, comes back empty and silently resets.
 *
 * The previous version guessed from `media.type` alone and disagreed in BOTH
 * directions: a viewable clip that ships `preview` but no `thumb` got no button
 * (OF has a poster for it), and a gif whose only file is the .mp4 got one that
 * could never work. Resolving a URL the same way the server does removes the
 * category of bug rather than patching the two known cases.
 *
 * Kept in step by a SHARED FIXTURE — `service/tests/fixtures/of_media_shapes.json`
 * is asserted by both this module's vitest suite and the python suite, over the
 * same payloads. Adding a case to the fixture fails whichever side drifts.
 */

import type { OFMedia, OFMediaFiles } from "@/lib/relay";

/** Extensions that are NOT a single decodable image — video AND audio.
 *
 *  Audio belongs here even though nobody would "look at" a voice note: the
 *  question is *is this an image*, and OF serves voice as a `files.full` ending
 *  .mp3 with no poster of any kind. `.mp4` matters just as much — OF stores a
 *  **gif** as an mp4, so a gif's `full` is a video however its `type` reads. */
const NON_IMAGE_EXTS = [
  ".mp4", ".m3u8", ".mov", ".webm", ".m4v",
  ".mp3", ".m4a", ".wav", ".aac", ".ogg", ".opus",
];

/** Image variants to prefer, widest-useful first — the same order, and for the
 *  same hard-won reasons, as `of_shapes._VISION_VARIANTS`: `thumb` is a 300x300
 *  centre-CROP of a 3:4 portrait, so it is the WRONG default however tempting;
 *  `preview` (~960px) keeps the real aspect ratio. For a VIDEO this list resolves
 *  to OF's own poster frame, because `preview` is a still while `full` is the
 *  clip (skipped by the non-image test). */
const VISION_VARIANTS: (keyof OFMediaFiles)[] = [
  "preview", "full", "source", "squarePreview", "thumb",
];

/** True when `url` points at video/audio rather than a decodable image. OF's CDN
 *  urls carry a long signed query, so the test runs on the path only. */
function isNonImageUrl(url: string | null | undefined): boolean {
  if (!url) return false;
  const path = url.split("?")[0].toLowerCase();
  return NON_IMAGE_EXTS.some((ext) => path.endsWith(ext));
}

/** The url of a single decodable IMAGE for this media item, or null.
 *
 *  Mirrors `of_shapes.still_url`. Null means "nothing renderable is on offer" —
 *  a locked clip (only the .mp4), a voice note, an item OF sent no files for. */
export function stillUrl(media: OFMedia | null | undefined): string | null {
  if (!media) return null;
  const files = media.files;
  if (files) {
    for (const key of VISION_VARIANTS) {
      const url = files[key]?.url;
      if (url && !isNonImageUrl(url)) return url;
    }
  }
  // Shape drift: some payloads put the url straight on the media object.
  const loose = media.url;
  return loose && !isNonImageUrl(loose) ? loose : null;
}

/** Can the vision path get a still out of ANY of this bubble's media? */
export function hasDescribableStill(media: OFMedia[] | undefined): boolean {
  return (media ?? []).some((m) => stillUrl(m) !== null);
}

/** Renderable URL for a GIF the OF wire format carries as a bare `giphyId`.
 *
 *  A GIF is the one attachment OF does NOT put in `media[]`: it rides as a
 *  top-level `giphyId` beside empty text, so `stillUrl` above has nothing to
 *  resolve and every surface that shows one has to rebuild this URL. Three did,
 *  by hand, in two files. One id → one URL belongs here with the rest of the
 *  OF-shape knowledge. */
export function giphyUrl(giphyId: string): string {
  // Fansly rides GIFs through KLIPY, not Giphy, and its ids aren't giphy ids —
  // the shim puts a full renderable url in `giphyId` (see fansly_shim of_message
  // / _klipy_to_giphy). Pass any http(s) value straight through; only a bare
  // giphy id gets the media.giphy.com wrapper.
  if (/^https?:\/\//i.test(giphyId)) return giphyId;
  return `https://media.giphy.com/media/${giphyId}/giphy.gif`;
}
