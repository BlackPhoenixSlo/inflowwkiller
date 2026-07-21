// Source-resolution rules for vault videos, locked against the real OF
// shapes captured from one account's vault (account ACCOUNT_ID, 2026-06-04):
//   • Old uploads ship a progressive mp4 under videoSources/files.full.
//   • Newer uploads are DRM-only (FairPlay): videoSources null,
//     files.full.sources [], real video behind files.drm — and the ONLY
//     renderable representation is OF's poster frames in preview.options[].
// The bug these guard against: the resolver fell through to files.preview.url
// (a static poster JPEG) for DRM items, so <video> and /img/scrub both failed
// ("Couldn't load" + 502 storyboard). DRM items must resolve to null
// progressive src + the poster-frame list instead.
import { describe, it, expect } from "vitest";
import type { VaultMedia } from "@/lib/relay";
import { progressiveVideoSrc, videoPosterFrames, isDrmVideo, isDrmOnlyVideo, evenFrameIndex } from "./VaultPicker";

const drmVideo: VaultMedia = {
  id: 4340233385,
  type: "video",
  duration: 14,
  videoSources: { "720": null, "240": null },
  files: {
    thumb: { url: "https://cdn2.onlyfans.com/.../300x300_frame_0.jpg" },
    full: { width: 576, height: 1024, sources: [] },
    preview: {
      url: "https://cdn2.onlyfans.com/.../576x1024_frame_0.jpg",
      options: [
        { id: 1, url: "https://cdn2.onlyfans.com/a/576x1024_frame_0.jpg" },
        { id: 2, url: "https://cdn2.onlyfans.com/b/576x1024_frame_1.jpg" },
        { id: 3, url: "https://cdn2.onlyfans.com/c/576x1024_frame_2.jpg" },
      ],
    },
    drm: { manifest: { hls: "https://cdn3.onlyfans.com/hls/.../x.m3u8", dash: "https://cdn3.onlyfans.com/dash/.../x.mpd" } },
  },
};

const progressiveVideo: VaultMedia = {
  id: 999,
  type: "video",
  duration: 8,
  videoSources: { "720": "https://cdn2.onlyfans.com/.../720.mp4", "240": null },
  files: { thumb: { url: "https://cdn2.onlyfans.com/.../t.jpg" } },
};

const photo: VaultMedia = {
  id: 4489341126,
  type: "photo",
  files: { full: { url: "https://cdn2.onlyfans.com/.../1852x746.jpg" } },
};

describe("vault video source resolution", () => {
  it("resolves a progressive mp4 for old uploads", () => {
    expect(progressiveVideoSrc(progressiveVideo)).toBe(
      "https://cdn2.onlyfans.com/.../720.mp4",
    );
    expect(isDrmVideo(progressiveVideo)).toBe(false);
  });

  it("prefers the cheapest source (240p) over 720p for scrub frames", () => {
    // Frames are scaled down to a small storyboard width anyway, so pulling
    // 720p is wasted bandwidth — 240p must win when both are present.
    const both: VaultMedia = {
      id: 1000,
      type: "video",
      duration: 8,
      videoSources: {
        "720": "https://cdn2.onlyfans.com/.../720.mp4",
        "240": "https://cdn2.onlyfans.com/.../240.mp4",
      },
      files: { thumb: { url: "https://cdn2.onlyfans.com/.../t.jpg" } },
    };
    expect(progressiveVideoSrc(both)).toBe("https://cdn2.onlyfans.com/.../240.mp4");
  });

  it("returns NULL progressive src for DRM-only videos (never the poster JPEG)", () => {
    // The core regression: must not fall through to files.preview.url.
    expect(progressiveVideoSrc(drmVideo)).toBeNull();
  });

  it("flags DRM via the authoritative files.drm manifest, exposes poster frames", () => {
    expect(isDrmVideo(drmVideo)).toBe(true);
    expect(isDrmOnlyVideo(drmVideo)).toBe(true);
    expect(videoPosterFrames(drmVideo)).toEqual([
      "https://cdn2.onlyfans.com/a/576x1024_frame_0.jpg",
      "https://cdn2.onlyfans.com/b/576x1024_frame_1.jpg",
      "https://cdn2.onlyfans.com/c/576x1024_frame_2.jpg",
    ]);
  });

  it("treats photos and non-video items as non-DRM with no progressive src", () => {
    expect(progressiveVideoSrc(photo)).toBeNull();
    expect(isDrmVideo(photo)).toBe(false);
    expect(videoPosterFrames(photo)).toEqual([]);
  });

  it("still flags DRM (via manifest) even when OF ships no poster frames", () => {
    // Edge case: a DRM file with an empty preview.options. It must still be
    // recognised as DRM (so the UI shows the locked state, not a broken
    // <video> or a 'transcoding' message), just without a slideshow.
    const noFrames: VaultMedia = {
      id: 1, type: "video", videoSources: { "720": null, "240": null },
      files: { drm: { manifest: { hls: "https://cdn3.onlyfans.com/hls/x.m3u8" } } },
    };
    expect(isDrmVideo(noFrames)).toBe(true);
    expect(isDrmOnlyVideo(noFrames)).toBe(true);
    expect(videoPosterFrames(noFrames)).toEqual([]);
  });

  it("spreads N poster frames evenly across the 12-beat cursor (no repeat-jump, no out-of-range)", () => {
    const seqFor = (n: number) =>
      Array.from({ length: 12 }, (_, beat) => evenFrameIndex(beat, n, 12));

    // Every available frame must appear, indices in range, and monotonic
    // (no backward jump that reads as a stutter/flash).
    for (const n of [1, 3, 5, 9, 12]) {
      const seq = seqFor(n);
      expect(Math.min(...seq)).toBe(0);
      expect(Math.max(...seq)).toBe(n - 1); // covers the last frame
      expect(new Set(seq).size).toBe(n); // every frame shown
      for (let i = 1; i < seq.length; i++) expect(seq[i]).toBeGreaterThanOrEqual(seq[i - 1]);
    }
    // 12 frames → identity (the eventual ffmpeg batch maps 1:1).
    expect(seqFor(12)).toEqual([0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]);
    // 5 frames → even hold, never the old `% 5` wrap (…3,4,0,1).
    expect(seqFor(5)).toEqual([0, 0, 0, 1, 1, 2, 2, 2, 3, 3, 4, 4]);
    // degenerate guard: 0 frames never indexes out of bounds.
    expect(evenFrameIndex(11, 0, 12)).toBe(0);
  });

  it("prefers playback when a DRM file unexpectedly also has a progressive mp4", () => {
    const both: VaultMedia = {
      id: 2, type: "video",
      videoSources: { "720": "https://cdn2.onlyfans.com/x/720.mp4", "240": null },
      files: { drm: { manifest: { hls: "https://cdn3.onlyfans.com/hls/x.m3u8" } } },
    };
    expect(isDrmVideo(both)).toBe(true); // it IS drm-delivered
    expect(isDrmOnlyVideo(both)).toBe(false); // ...but we can still play it
    expect(progressiveVideoSrc(both)).toBe("https://cdn2.onlyfans.com/x/720.mp4");
  });
});
