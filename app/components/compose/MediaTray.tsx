"use client";

/**
 * MediaTray — shared attachment strip for the New-post + Mass-message
 * modals. Renders thumbnails for already-attached vault media plus an
 * "add from vault" button (lucide Library).
 *
 * Upload-from-computer is intentionally removed for now (the producer
 * pipeline has open bugs; revisit once those are fixed). Vault-pick +
 * reorder + PPV preview-count UI remain functional.
 */

import { Fragment } from "react";
import { Library } from "lucide-react";

import { useReorder } from "@/hooks/useReorder";
import { proxyImage, type VaultMedia } from "@/lib/relay";
import { cn } from "@/lib/utils";

export function MediaTray({
  accountId, attached, onChange, onOpenVaultPicker,
  price, previewCount, onPreviewCountChange,
}: {
  accountId: string | null;
  attached: VaultMedia[];
  onChange: (next: VaultMedia[]) => void;
  onOpenVaultPicker: () => void;
  /** Per-message PPV price (dollars). When > 0, tiles show FREE/PAID
   *  badges + a divider chip. Omit for free-only callers. */
  price?: number;
  /** How many leading tiles are free previews. Required alongside `price`
   *  for the badge/divider UI to render. */
  previewCount?: number;
  onPreviewCountChange?: (n: number) => void;
}) {
  const reorder = useReorder(attached, onChange, (m) => m.id);
  const numericPrice = typeof price === "number" && Number.isFinite(price) ? price : 0;
  const showPpv = numericPrice > 0 && typeof previewCount === "number";
  const effectivePreviewCount = showPpv
    ? Math.max(0, Math.min(previewCount, attached.length))
    : 0;
  const showDivider =
    showPpv && effectivePreviewCount > 0 && effectivePreviewCount < attached.length;
  const priceLabel = numericPrice > 0
    ? `$${numericPrice % 1 === 0 ? numericPrice : numericPrice.toFixed(2)}`
    : "";

  function remove(id: number) {
    onChange(attached.filter((m) => m.id !== id));
  }

  return (
    <div className="space-y-1.5">
      {attached.length > 0 ? (
        <div className="flex items-center gap-1.5 flex-wrap bg-bg/60 border border-border rounded-lg px-2 py-2">
          {showPpv && onPreviewCountChange && (
            <div
              className="flex items-center gap-1 text-[10px] text-fg-dim mr-1"
              title="How many tiles are free previews. Drag a tile across the divider to flip its side."
            >
              <span className="uppercase tracking-wide">previews</span>
              <button
                type="button"
                onClick={() => onPreviewCountChange(Math.max(0, effectivePreviewCount - 1))}
                disabled={effectivePreviewCount === 0}
                className="w-4 h-4 grid place-items-center rounded border border-border hover:bg-bg-elev-1 disabled:opacity-30"
                aria-label="Fewer free previews"
              >−</button>
              <span className="tabular-nums text-fg">
                {effectivePreviewCount}/{attached.length}
              </span>
              <button
                type="button"
                onClick={() => onPreviewCountChange(Math.min(attached.length, effectivePreviewCount + 1))}
                disabled={effectivePreviewCount >= attached.length}
                className="w-4 h-4 grid place-items-center rounded border border-border hover:bg-bg-elev-1 disabled:opacity-30"
                aria-label="More free previews"
              >+</button>
            </div>
          )}
          {attached.map((m, idx) => {
            const rawThumb =
              m.files?.thumb?.url ||
              m.files?.squarePreview?.url ||
              m.files?.preview?.url ||
              null;
            const thumb = proxyImage(rawThumb, accountId);
            const isFreeTile = idx < effectivePreviewCount;
            const isDragging = reorder.draggingIndex === idx;
            const isDragOver = reorder.dragOverIndex === idx && !isDragging;
            return (
              <Fragment key={m.id}>
                {showDivider && idx === effectivePreviewCount && (
                  <div
                    className="flex flex-col items-center justify-center gap-0.5 h-16 px-1 text-[8px] font-semibold uppercase tracking-wide select-none border-l-2 border-r-2 border-dashed border-fg-dim/60"
                    title="Items left of this line are free previews; items right are PPV-locked"
                    aria-hidden
                  >
                    <span className="text-fg-dim leading-none">free ↑</span>
                    <span className="text-muted leading-none">/</span>
                    <span className="text-err leading-none">paid ↓</span>
                  </div>
                )}
                <div
                  draggable
                  onDragStart={(e) => reorder.onDragStart(e, idx)}
                  onDragOver={(e) => reorder.onDragOver(e, idx)}
                  onDrop={(e) => reorder.onDrop(e, idx)}
                  onDragEnd={reorder.onDragEnd}
                  tabIndex={0}
                  onKeyDown={(e) => {
                    if (e.key === "ArrowLeft") {
                      e.preventDefault();
                      reorder.onKeyboardMove(idx, -1);
                    } else if (e.key === "ArrowRight") {
                      e.preventDefault();
                      reorder.onKeyboardMove(idx, 1);
                    }
                  }}
                  className={cn(
                    "relative w-16 h-16 rounded-md overflow-hidden border bg-bg-elev-1 select-none",
                    "motion-safe:transition-transform motion-safe:duration-150",
                    "cursor-grab active:cursor-grabbing",
                    "focus:outline-none focus:ring-2 focus:ring-accent",
                    isDragging ? "opacity-40 border-accent" : "border-border",
                    isDragOver && "ring-2 ring-accent",
                  )}
                >
                  {thumb ? (
                    <img src={thumb} alt="" loading="lazy" decoding="async" draggable={false} className="w-full h-full object-cover pointer-events-none" />
                  ) : (
                    <div className="w-full h-full grid place-items-center text-[10px] text-fg-dim">
                      {m.type}
                    </div>
                  )}
                  {showPpv && (
                    <div
                      className={cn(
                        "absolute top-0.5 left-0.5 px-1 rounded text-[9px] font-bold leading-tight text-white shadow",
                        isFreeTile ? "bg-fg-dim" : "bg-err",
                      )}
                      title={isFreeTile ? "Free preview" : "PPV-locked"}
                    >
                      {isFreeTile ? "FREE" : priceLabel || "PPV"}
                    </div>
                  )}
                  <button
                    type="button"
                    onClick={() => remove(m.id)}
                    className="absolute top-0.5 right-0.5 w-4 h-4 rounded-full bg-black/70 text-white grid place-items-center text-[10px] opacity-90"
                    aria-label="Remove"
                  >×</button>
                </div>
              </Fragment>
            );
          })}
          <button
            type="button"
            onClick={onOpenVaultPicker}
            disabled={!accountId}
            className="w-16 h-16 rounded-md border border-dashed border-border hover:bg-bg-elev-1 text-fg-dim hover:text-fg grid place-items-center disabled:opacity-50"
            title="Pick from vault"
          >
            <Library size={18} aria-hidden />
          </button>
        </div>
      ) : (
        <button
          type="button"
          onClick={onOpenVaultPicker}
          disabled={!accountId}
          className="w-full text-xs text-fg-dim hover:text-fg border border-dashed border-border rounded-md py-3 hover:bg-bg-elev-1 disabled:opacity-50 inline-flex items-center justify-center gap-1.5"
        >
          <Library size={18} aria-hidden />
          Add from vault
        </button>
      )}
    </div>
  );
}
