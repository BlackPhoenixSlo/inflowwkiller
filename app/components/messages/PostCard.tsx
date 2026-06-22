"use client";

/**
 * PostCard — one feed post, shared by the Posts (home timeline) and
 * My Feed (own profile wall) tabs so both render identically.
 */

import { useState } from "react";

import { postPlainText, postThumbUrl, type OFPost } from "@/hooks/usePosts";
import { proxyImage } from "@/lib/relay";

function priceLabel(price: OFPost["price"]): string | null {
  const n = typeof price === "string" ? Number(price) : price ?? 0;
  if (!Number.isFinite(n) || (n as number) <= 0) return null;
  return `$${n}`;
}

interface PostCardProps {
  post: OFPost;
  accountId: string;
  /** When provided, a Delete control is rendered (with an inline confirm).
   *  Omit on read-only surfaces like the home-timeline Posts tab where the
   *  posts aren't ours to remove. */
  onDelete?: (postId: number) => void;
  /** True while THIS post's delete request is in flight. */
  deleting?: boolean;
  /** When provided, an Edit control opens the caller's editor for this post. */
  onEdit?: (post: OFPost) => void;
  /** When provided, a Pin/Unpin toggle is rendered (reads post.isPinned). */
  onTogglePin?: (post: OFPost) => void;
  /** True while THIS post's pin/unpin request is in flight. */
  pinning?: boolean;
  /** Rank position (1-based) for the Top Posts view; renders a "#N" chip. */
  rank?: number;
}

export function PostCard({
  post,
  accountId,
  onDelete,
  deleting,
  onEdit,
  onTogglePin,
  pinning,
  rank,
}: PostCardProps) {
  const text = postPlainText(post);
  const media = (post.media ?? []).filter(Boolean);
  const price = priceLabel(post.price);

  // Paywall state is post-level: a PAID post locks every media item except
  // the ids listed in `previews` (free teasers). For a fan view, OF also
  // sets `canView:false` on media they haven't unlocked. We only badge
  // images when there's actually a paywall (paid post) so free posts stay
  // clean.
  const priceNum = typeof post.price === "string" ? Number(post.price) : post.price ?? 0;
  const isPaidPost = Number.isFinite(priceNum) && (priceNum as number) > 0;
  const previewIds = new Set(
    [...(post.previews ?? []), ...(post.preview ?? [])].filter((id) => typeof id === "number"),
  );
  const isLocked = (m: { id: number; canView?: boolean }) =>
    m.canView === false || (isPaidPost && !previewIds.has(m.id));
  const when = post.postedAt || post.postedAtPrecise;
  const dateStr = when
    ? new Date(when).toLocaleDateString(undefined, {
        month: "short",
        day: "numeric",
        year: "numeric",
      })
    : "";

  return (
    <article className="border border-border rounded-xl bg-panel p-3 space-y-2">
      <div className="flex items-center gap-2 text-[11px] text-fg-dim">
        {rank != null && (
          <span className="px-1.5 py-0.5 rounded bg-info/15 text-info font-semibold">#{rank}</span>
        )}
        {post.isPinned && (
          <span className="px-1.5 py-0.5 rounded bg-accent/15 text-accent font-medium">Pinned</span>
        )}
        {price && (
          <span className="px-1.5 py-0.5 rounded bg-ok/15 text-ok font-medium">{price}</span>
        )}
        <span>{dateStr}</span>
        {(onTogglePin || onEdit || onDelete) && (
          <div className="ml-auto flex items-center gap-3">
            {onTogglePin && (
              <button
                type="button"
                onClick={() => onTogglePin(post)}
                disabled={pinning}
                className="text-[11px] text-fg-dim hover:text-accent transition-colors disabled:opacity-50"
              >
                {pinning ? "…" : post.isPinned ? "Unpin" : "Pin"}
              </button>
            )}
            {onEdit && (
              <button
                type="button"
                onClick={() => onEdit(post)}
                className="text-[11px] text-fg-dim hover:text-fg transition-colors"
              >
                Edit
              </button>
            )}
            {onDelete && (
              <DeleteControl deleting={!!deleting} onConfirm={() => onDelete(post.id)} />
            )}
          </div>
        )}
      </div>

      {text && <p className="text-sm text-fg whitespace-pre-wrap break-words">{text}</p>}

      {media.length > 0 && (
        <div className="grid grid-cols-3 sm:grid-cols-4 gap-1.5">
          {media.slice(0, 8).map((m) => {
            const url = postThumbUrl(m);
            return (
              <div
                key={m.id}
                className="relative aspect-square rounded-lg overflow-hidden bg-bg border border-border"
              >
                {url ? (
                  // eslint-disable-next-line @next/next/no-img-element
                  <img
                    src={proxyImage(url, accountId)}
                    alt=""
                    loading="lazy"
                    className="w-full h-full object-cover"
                  />
                ) : (
                  <div className="w-full h-full flex items-center justify-center text-[10px] text-fg-dim">
                    {m.type || "media"}
                  </div>
                )}
                {(m.type === "video" || m.type === "gif") && (
                  <span className="absolute bottom-1 right-1 text-[9px] px-1 rounded bg-black/60 text-white uppercase">
                    {m.type}
                  </span>
                )}
                {isPaidPost && (
                  isLocked(m) ? (
                    <span className="absolute top-1 left-1 text-[9px] px-1 py-0.5 rounded bg-pink/90 text-white font-medium">
                      🔒 Paid
                    </span>
                  ) : (
                    <span className="absolute top-1 left-1 text-[9px] px-1 py-0.5 rounded bg-ok/90 text-white font-medium">
                      Free
                    </span>
                  )
                )}
              </div>
            );
          })}
        </div>
      )}

      <div className="flex items-center gap-4 text-[11px] text-fg-dim pt-0.5">
        <span>♥ {post.favoritesCount ?? post.likesCount ?? 0}</span>
        <span>💬 {post.commentsCount ?? 0}</span>
        {media.length > 8 && <span>+{media.length - 8} more</span>}
        {post.tipsAmount && post.tipsAmount !== "$0" && <span>tips {post.tipsAmount}</span>}
      </div>
    </article>
  );
}

/** Two-step delete: "Delete" → "Confirm? / Cancel". Avoids a native
 *  confirm() dialog and an accidental one-click removal of a live post. */
function DeleteControl({
  deleting,
  onConfirm,
}: {
  deleting: boolean;
  onConfirm: () => void;
}) {
  const [armed, setArmed] = useState(false);

  if (deleting) {
    return <span className="text-[11px] text-err">Deleting…</span>;
  }

  if (!armed) {
    return (
      <button
        type="button"
        onClick={() => setArmed(true)}
        className="text-[11px] text-fg-dim hover:text-err transition-colors"
      >
        Delete
      </button>
    );
  }

  return (
    <span className="flex items-center gap-2">
      <button
        type="button"
        onClick={onConfirm}
        className="text-[11px] font-medium text-err hover:underline"
      >
        Confirm
      </button>
      <button
        type="button"
        onClick={() => setArmed(false)}
        className="text-[11px] text-fg-dim hover:text-fg"
      >
        Cancel
      </button>
    </span>
  );
}
