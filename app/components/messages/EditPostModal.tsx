"use client";

/**
 * EditPostModal — lightweight editor for an existing feed post's TEXT and
 * PRICE via PUT /api/of/v2/posts/{id}.
 *
 * Deliberately scoped to text + price (the common edits). Re-attaching /
 * reordering media is intentionally NOT here: the feed payload only gives
 * us media id stubs, and a partial media_files array would drop the rest
 * of the post's media on OF. The relay does a sparse update, so omitting
 * media_files leaves the existing media untouched.
 */

import { useState } from "react";
import { useMutation } from "@tanstack/react-query";

import { Button, Input } from "@/components/ui/primitives";
import { sanitizePriceInput } from "@/components/chat/Composer";
import { postPlainText, type OFPost } from "@/hooks/usePosts";
import { relay } from "@/lib/relay";

interface Props {
  post: OFPost;
  accountId: string;
  onClose: () => void;
  onSaved: (patch: { text: string; price: number }) => void;
}

export default function EditPostModal({ post, accountId, onClose, onSaved }: Props) {
  const initialText = post.rawText ?? postPlainText(post);
  const initialPrice =
    typeof post.price === "string" ? post.price : post.price != null ? String(post.price) : "";

  const [text, setText] = useState(initialText);
  const [price, setPrice] = useState(initialPrice);
  const [error, setError] = useState<string | null>(null);

  const save = useMutation({
    mutationFn: async () => {
      const priceNum = price ? Number(price) : 0;
      if (!Number.isFinite(priceNum) || priceNum < 0) throw new Error("Invalid price");
      const trimmed = text.trim();
      if (!trimmed) throw new Error("Text can't be empty");

      const body: Record<string, unknown> = { text: trimmed, price: priceNum };

      if (priceNum > 0) {
        // OF rejects a paid post that doesn't declare its protected media
        // ("Paid post must contain protected content"). A sparse PUT with
        // only price omits that, so we must re-send the media to lock. Pull
        // the AUTHORITATIVE media set from the post detail rather than the
        // (possibly lossy) feed payload — sending a partial list would drop
        // the rest of the post's media on OF. Preserve existing free
        // previews so re-pricing doesn't silently lock the teasers.
        const detail = await relay.get<{
          media?: { id: number }[];
          previews?: number[];
        }>(`/api/of/v2/posts/${post.id}`, { accountId });
        const mediaIds = (detail.media ?? [])
          .map((m) => m.id)
          .filter((id) => typeof id === "number" && Number.isFinite(id));
        if (mediaIds.length === 0) {
          throw new Error(
            "This is a text-only post — OnlyFans can't charge for it. Set the price to 0, or add media to the post on OnlyFans first.",
          );
        }
        body.media_files = mediaIds;
        if (Array.isArray(detail.previews) && detail.previews.length > 0) {
          body.previews = detail.previews;
        }
      }

      await relay.put(`/api/of/v2/posts/${post.id}`, body, { accountId });
      return { text: trimmed, price: priceNum };
    },
    onSuccess: (patch) => {
      onSaved(patch);
      onClose();
    },
    onError: (err: Error) => setError(err.message),
  });

  return (
    <div
      className="fixed inset-0 z-50 bg-black/50 grid place-items-center p-4"
      onClick={() => { if (!save.isPending) onClose(); }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="w-full max-w-[520px] max-h-[90vh] flex flex-col bg-panel border border-border rounded-xl shadow-2xl"
      >
        <header className="px-4 py-3 border-b border-border flex items-center justify-between">
          <h2 className="text-sm font-semibold">Edit post</h2>
          <button
            type="button"
            onClick={() => { if (!save.isPending) onClose(); }}
            className="text-fg-dim hover:text-fg text-lg leading-none"
            title="Close"
          >×</button>
        </header>

        <div className="flex-1 overflow-y-auto p-4 space-y-3">
          <label className="flex flex-col gap-1">
            <span className="text-[10px] uppercase tracking-wide text-fg-dim">Text</span>
            <textarea
              value={text}
              onChange={(e) => setText(e.target.value)}
              rows={5}
              className="w-full bg-bg border border-border rounded-lg px-3 py-2 text-sm text-fg focus:outline-none focus:border-accent resize-y"
              placeholder="Post text…"
            />
          </label>

          <label className="flex flex-col gap-1 w-40">
            <span className="text-[10px] uppercase tracking-wide text-fg-dim">Price ($, 0 = free)</span>
            <Input
              value={price}
              onChange={(e) => setPrice(sanitizePriceInput(e.target.value))}
              inputMode="decimal"
              placeholder="0"
              className="text-sm"
            />
          </label>

          <p className="text-[11px] text-fg-dim">
            Edits the live OnlyFans post. Setting a price locks the post&apos;s existing media behind
            the paywall (a text-only post can&apos;t be priced); price 0 makes it free again.
          </p>

          {error && (
            <div className="p-2.5 text-sm text-err border border-err/30 bg-err/10 rounded-lg">
              {error}
            </div>
          )}
        </div>

        <footer className="px-4 py-3 border-t border-border flex items-center justify-end gap-2">
          <Button type="button" variant="secondary" size="sm" onClick={onClose} disabled={save.isPending}>
            Cancel
          </Button>
          <Button
            type="button"
            variant="primary"
            size="sm"
            onClick={() => { setError(null); save.mutate(); }}
            disabled={save.isPending}
          >
            {save.isPending ? "Saving…" : "Save"}
          </Button>
        </footer>
      </div>
    </div>
  );
}
