"use client";

/**
 * VaultTile — one media tile with hover preview + select/describe/order affordances.
 *
 * Hover: video cycles OF poster frames → HD storyboard (via /img/scrub), cancel
 * on leave. Optional selection checkbox, a describe-status dot, and a per-folder
 * manual-order input (0,1,2,3 / -1,-2,-3) shown only in a folder view.
 */

import { useEffect, useRef, useState } from "react";

import {
  progressiveVideoSrc,
  videoPosterFrames,
  isDrmOnlyVideo,
  evenFrameIndex,
  aiCaptionFor,
  useCaptionOverlay,
  VaultAiCaptionOverlay,
} from "@/components/chat/VaultPicker";
import { proxyImage, proxyScrubFrame, type VaultMedia } from "@/lib/relay";
import { fmtDuration } from "@/lib/format";

const SCRUB_FRAMES = 12;
const SCRUB_INTERVAL_MS = 450;
const HOVER_DELAY_MS = 400;

function rawThumb(m: VaultMedia): string | undefined {
  return (
    m.files?.thumb?.url ||
    m.files?.squarePreview?.url ||
    m.files?.preview?.url ||
    m.files?.full?.url ||
    undefined
  );
}

interface AiMeta {
  describe_status?: string | null;
  manual_order?: number | null;
  description?: string | null;
  video_description?: string | null;
  tags?: string[] | null;
}

export interface VaultTileProps {
  media: VaultMedia & { _ai?: AiMeta };
  accountId: string | null;
  onClick?: (m: VaultMedia) => void;
  /** When set, the duration pill becomes a play button that opens the picker's
   *  full-screen VaultVideoPreview (one click, same as the chat picker). */
  onPlay?: (m: VaultMedia) => void;
  selected?: boolean;
  selectMode?: boolean;
  checked?: boolean;
  onCheck?: (m: VaultMedia) => void;
  showOrder?: boolean;
  onOrder?: (m: VaultMedia, value: number | null) => void;
  /** Force the AI caption overlay on/off. When omitted, follows the persisted
   *  manager toggle (default ON). */
  aiCaption?: boolean;
}

export default function VaultTile({
  media: m,
  accountId,
  onClick,
  onPlay,
  selected,
  selectMode,
  checked,
  onCheck,
  showOrder,
  onOrder,
  aiCaption,
}: VaultTileProps) {
  const [managerCaptionOn] = useCaptionOverlay("manager");
  const captionOn = aiCaption ?? managerCaptionOn;
  const caption = captionOn ? aiCaptionFor(m) : null;
  const [hovering, setHovering] = useState(false);
  const [scrubIdx, setScrubIdx] = useState(0);
  const [scrubReady, setScrubReady] = useState(false);
  const [sessionId, setSessionId] = useState<string>("");

  const dwellRef = useRef<number | null>(null);
  const slideRef = useRef<number | null>(null);
  const hoveringRef = useRef(false);

  // Prefer the relay's cached 300px thumb (permanent, signature-free) over the
  // expiring OF url — that's what fixes broken tiles on mirror reads.
  const cachedThumb = (m as unknown as { _thumb?: string })._thumb;
  const thumb = cachedThumb || proxyImage(rawThumb(m), accountId);
  const dur = fmtDuration(m.duration);
  const isVideo = m.type === "video";
  const rawVideoSrc = isVideo ? progressiveVideoSrc(m) : null;
  const posterFrames = isVideo ? videoPosterFrames(m) : [];
  const drmOnly = isVideo && isDrmOnlyVideo(m);
  const canScrub = isVideo && !!rawVideoSrc;
  const described = m._ai?.describe_status === "described";

  function startHover() {
    if (!isVideo) return;
    if (dwellRef.current != null) window.clearTimeout(dwellRef.current);
    dwellRef.current = window.setTimeout(() => {
      dwellRef.current = null;
      const sid =
        typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
          ? crypto.randomUUID()
          : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
      setSessionId(sid);
      setScrubIdx(0);
      setScrubReady(false);
      hoveringRef.current = true;
      setHovering(true);
    }, HOVER_DELAY_MS);
  }

  function endHover() {
    if (dwellRef.current != null) {
      window.clearTimeout(dwellRef.current);
      dwellRef.current = null;
    }
    if (!hoveringRef.current) return;
    hoveringRef.current = false;
    setHovering(false);
    if (rawVideoSrc && accountId) {
      const tok =
        typeof window !== "undefined"
          ? new URLSearchParams(window.location.search).get("t") ||
            window.localStorage.getItem("chatterly:share_token")
          : null;
      const qs = new URLSearchParams();
      qs.set("u", rawVideoSrc);
      if (sessionId) qs.set("sid", sessionId);
      if (tok) qs.set("t", tok);
      fetch(`/img/scrub/cancel?${qs.toString()}`, { method: "POST", keepalive: true }).catch(
        () => {},
      );
    }
  }

  useEffect(() => {
    if (!hovering) return;
    slideRef.current = window.setInterval(() => {
      setScrubIdx((n) => (n + 1) % SCRUB_FRAMES);
    }, SCRUB_INTERVAL_MS);
    return () => {
      if (slideRef.current != null) window.clearInterval(slideRef.current);
    };
  }, [hovering]);

  useEffect(() => {
    return () => {
      if (dwellRef.current != null) window.clearTimeout(dwellRef.current);
      if (slideRef.current != null) window.clearInterval(slideRef.current);
    };
  }, []);

  const showPoster = hovering && posterFrames.length > 0;
  const posterIdx = showPoster ? evenFrameIndex(scrubIdx, posterFrames.length) : 0;
  const showScrub = hovering && canScrub;

  function handleClick() {
    if (selectMode) onCheck?.(m);
    else onClick?.(m);
  }

  return (
    <div
      onPointerEnter={(e) => {
        if (e.pointerType === "mouse") startHover();
      }}
      onPointerLeave={(e) => {
        if (e.pointerType === "mouse") endHover();
      }}
      onClick={handleClick}
      title={`#${m.id} · ${m.type}${dur ? ` · ${dur}` : ""}`}
      className={`relative aspect-square rounded-lg overflow-hidden bg-bg-elev-1 border cursor-pointer transition-colors ${
        selected || checked ? "border-accent ring-2 ring-accent/40" : "border-border hover:border-fg-dim/50"
      }`}
    >
      {thumb ? (
        // eslint-disable-next-line @next/next/no-img-element
        <img
          src={thumb}
          alt=""
          aria-hidden
          loading="lazy"
          decoding="async"
          className="absolute inset-0 w-full h-full object-cover"
        />
      ) : (
        <div className="absolute inset-0 grid place-items-center text-[10px] text-fg-dim">{m.type}</div>
      )}

      {showPoster &&
        posterFrames.map((u, idx) => (
          // eslint-disable-next-line @next/next/no-img-element
          <img
            key={`pf-${idx}`}
            src={proxyImage(u, accountId)}
            alt=""
            aria-hidden
            decoding="async"
            style={{ opacity: idx === posterIdx ? 1 : 0 }}
            className="absolute inset-0 w-full h-full object-cover transition-opacity duration-200"
          />
        ))}

      {showScrub &&
        Array.from({ length: SCRUB_FRAMES }).map((_, idx) => (
          // eslint-disable-next-line @next/next/no-img-element
          <img
            key={`sf-${idx}`}
            src={proxyScrubFrame(rawVideoSrc, accountId, idx, m.duration, sessionId)}
            alt=""
            aria-hidden
            decoding="async"
            onLoad={idx === 0 ? () => setScrubReady(true) : undefined}
            style={{ opacity: idx === scrubIdx && scrubReady ? 1 : 0 }}
            className="absolute inset-0 w-full h-full object-cover transition-opacity duration-200"
          />
        ))}

      {caption && <VaultAiCaptionOverlay caption={caption} />}

      {/* selection checkbox */}
      {selectMode && (
        <div
          className={`absolute top-1 right-1 w-5 h-5 rounded-full border-2 grid place-items-center text-[11px] ${
            checked ? "bg-accent border-accent text-white" : "bg-black/40 border-white/70 text-transparent"
          }`}
        >
          ✓
        </div>
      )}

      {/* per-folder manual order input */}
      {showOrder && (
        <input
          type="number"
          defaultValue={m._ai?.manual_order ?? ""}
          onClick={(e) => e.stopPropagation()}
          onBlur={(e) => {
            const v = e.target.value.trim();
            onOrder?.(m, v === "" ? null : Number(v));
          }}
          placeholder="#"
          className="hidden md:block absolute top-1 left-1 w-10 px-1 py-0.5 rounded bg-black/60 text-white text-[11px] border border-white/40 outline-none"
        />
      )}

      {/* described dot — in a folder view the order input owns this corner on
       *  desktop, but that input is hidden on phone, so the dot takes it back. */}
      {described && (
        <div
          className={`absolute top-1 left-1 w-2.5 h-2.5 rounded-full bg-emerald-400 ring-1 ring-black/40${
            showOrder ? " md:hidden" : ""
          }`}
          title="described"
        />
      )}

      {/* The duration pill IS the play affordance, same as the picker. A nested
       *  <button> isn't legal HTML inside the clickable tile, so role="button" +
       *  stopPropagation keeps tile-click = open detail and pill-click = play. */}
      {isVideo && onPlay && (
        <span
          role="button"
          tabIndex={0}
          onClick={(e) => {
            e.stopPropagation();
            onPlay(m);
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter" || e.key === " ") {
              e.preventDefault();
              e.stopPropagation();
              onPlay(m);
            }
          }}
          title="Preview video"
          className="absolute bottom-1 right-1 px-1.5 py-0.5 rounded bg-black/60 hover:bg-black/85 text-white text-[10px] leading-none cursor-pointer select-none"
        >
          ▶ {dur}
        </span>
      )}
      {isVideo && !onPlay && (
        <div className="absolute bottom-1 right-1 px-1.5 py-0.5 rounded bg-black/60 text-white text-[10px] leading-none pointer-events-none">
          {dur || "▶"}
        </div>
      )}
      {m.type === "gif" && (
        <div className="absolute bottom-1 right-1 px-1.5 py-0.5 rounded bg-black/60 text-white text-[10px] leading-none pointer-events-none">
          GIF
        </div>
      )}
      {drmOnly && (
        <div className="absolute bottom-1 left-1 px-1 py-0.5 rounded bg-amber-600/80 text-white text-[9px] leading-none pointer-events-none">
          DRM
        </div>
      )}
    </div>
  );
}
