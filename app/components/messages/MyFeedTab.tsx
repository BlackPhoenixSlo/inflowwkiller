"use client";

/**
 * MyFeedTab — the model's OWN profile wall (only their posts), as opposed
 * to the Posts tab which shows OF's home timeline (mixed creators).
 *
 * Owns a single-model picker (defaults to first active account) plus a
 * media-type filter. Pages via OF's tailMarker cursor through useMyFeed.
 * Per-post actions (delete / edit / pin) act on the live OnlyFans post and
 * patch the react-query cache optimistically so the list updates without a
 * full refetch.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQueryClient, type InfiniteData } from "@tanstack/react-query";

import { Button } from "@/components/ui/primitives";
import { PostCard } from "@/components/messages/PostCard";
import EditPostModal from "@/components/messages/EditPostModal";
import { PostComposer } from "@/components/compose/PostComposer";
import { useActiveAccounts } from "@/hooks/useAccounts";
import { useMyFeed, type FeedMediaType, type OFPost } from "@/hooks/usePosts";
import { relay } from "@/lib/relay";
import { cn } from "@/lib/utils";

// Mirror of useMyFeed's page shape, for typing the optimistic cache edits.
interface FeedPage {
  posts: OFPost[];
  hasMore: boolean;
  nextCursor: string | null;
}

const TYPES: { key: FeedMediaType; label: string }[] = [
  { key: "all", label: "All" },
  { key: "photo", label: "Photos" },
  { key: "video", label: "Videos" },
];

export default function MyFeedTab() {
  const accounts = useActiveAccounts();
  const [accountId, setAccountId] = useState<string | null>(null);
  const [type, setType] = useState<FeedMediaType>("all");
  const [newPostOpen, setNewPostOpen] = useState(false);
  const [editingPost, setEditingPost] = useState<OFPost | null>(null);

  useEffect(() => {
    if (accountId == null && accounts.length > 0) setAccountId(accounts[0].id);
  }, [accounts, accountId]);

  const q = useMyFeed(accountId, type);
  const posts = useMemo(
    () => (q.data?.pages ?? []).flatMap((p) => p.posts),
    [q.data],
  );

  const qc = useQueryClient();
  const queryKey = useMemo(() => ["my-feed", accountId, type], [accountId, type]);
  const ctx = { accountId: accountId ?? undefined };

  // Helper: rewrite the matching post across every cached page.
  const patchCache = (postId: number, fn: (p: OFPost) => OFPost) =>
    qc.setQueryData<InfiniteData<FeedPage>>(queryKey, (old) => {
      if (!old) return old;
      return {
        ...old,
        pages: old.pages.map((pg) => ({
          ...pg,
          posts: pg.posts.map((p) => (p.id === postId ? fn(p) : p)),
        })),
      };
    });

  const del = useMutation({
    mutationFn: (postId: number) => relay.delete(`/api/of/v2/posts/${postId}`, ctx),
    // Optimistically drop the row from every cached page; no refetch needed.
    onSuccess: (_data, postId) => {
      qc.setQueryData<InfiniteData<FeedPage>>(queryKey, (old) => {
        if (!old) return old;
        return {
          ...old,
          pages: old.pages.map((pg) => ({
            ...pg,
            posts: pg.posts.filter((p) => p.id !== postId),
          })),
        };
      });
    },
  });

  const pin = useMutation({
    mutationFn: (post: OFPost) => {
      const path = `/api/of/v2/posts/${post.id}/pin`;
      return post.isPinned ? relay.delete(path, ctx) : relay.post(path, undefined, ctx);
    },
    onSuccess: (_data, post) => patchCache(post.id, (p) => ({ ...p, isPinned: !post.isPinned })),
  });

  const sentinelRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    const el = sentinelRef.current;
    if (!el) return;
    const io = new IntersectionObserver((entries) => {
      if (entries[0]?.isIntersecting && q.hasNextPage && !q.isFetchingNextPage) {
        q.fetchNextPage();
      }
    }, { rootMargin: "200px" });
    io.observe(el);
    return () => io.disconnect();
  }, [q.hasNextPage, q.isFetchingNextPage, q.fetchNextPage]);

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-3 p-3 bg-panel border border-border rounded-xl">
        <label className="flex flex-col gap-1">
          <span className="text-[10px] uppercase tracking-wide text-fg-dim">Model</span>
          <select
            value={accountId ?? ""}
            onChange={(e) => setAccountId(e.target.value || null)}
            className="bg-bg border border-border rounded-lg px-3 py-2 text-base md:px-2 md:py-1.5 md:text-xs text-fg focus:outline-none focus:border-accent"
          >
            {accounts.length === 0 && <option value="">No models</option>}
            {accounts.map((a) => (
              <option key={a.id} value={a.id}>
                {a.nickname || a.id}
              </option>
            ))}
          </select>
        </label>

        <label className="flex flex-col gap-1 w-full md:w-auto">
          <span className="text-[10px] uppercase tracking-wide text-fg-dim">Type</span>
          <div className="grid grid-cols-3 gap-1 md:flex md:items-center">
            {TYPES.map((t) => (
              <Button
                key={t.key}
                type="button"
                variant={type === t.key ? "primary" : "secondary"}
                size="sm"
                onClick={() => setType(t.key)}
                className={cn("px-1.5 py-2.5 md:px-3 md:py-1.5", type !== t.key && "border-border")}
              >
                {t.label}
              </Button>
            ))}
          </div>
        </label>

        <div className="flex items-center gap-2 self-end ml-auto">
          <Button
            type="button"
            variant="secondary"
            size="sm"
            onClick={() => q.refetch()}
            disabled={!accountId || q.isFetching}
            className="hidden md:inline-flex border-border"
          >
            {q.isFetching ? "Loading…" : "Refresh"}
          </Button>
          <Button
            type="button"
            variant="primary"
            size="sm"
            onClick={() => setNewPostOpen(true)}
          >
            + New post
          </Button>
        </div>
      </div>

      {q.isError && (
        <div className="p-4 text-sm text-err border border-err/30 bg-err/10 rounded-xl">
          Failed to load feed: {(q.error as Error)?.message || "unknown error"}
        </div>
      )}

      {(del.isError || pin.isError) && (
        <div className="p-3 text-sm text-err border border-err/30 bg-err/10 rounded-xl">
          {del.isError
            ? `Delete failed: ${(del.error as Error)?.message || "unknown error"}`
            : `Pin failed: ${(pin.error as Error)?.message || "unknown error"}`}
        </div>
      )}

      {!accountId ? (
        <div className="p-8 text-center text-sm text-fg-dim border border-border rounded-xl bg-panel">
          Pick a model to see their profile feed.
        </div>
      ) : q.isPending ? (
        <div className="p-8 text-center text-sm text-fg-dim">Loading…</div>
      ) : posts.length === 0 ? (
        <div className="p-8 text-center text-sm text-fg-dim border border-border rounded-xl bg-panel">
          No posts on this profile yet.
        </div>
      ) : (
        <div className="space-y-3">
          {posts.map((p) => (
            <PostCard
              key={p.id}
              post={p}
              accountId={accountId}
              onDelete={(id) => del.mutate(id)}
              deleting={del.isPending && del.variables === p.id}
              onEdit={(post) => setEditingPost(post)}
              onTogglePin={(post) => pin.mutate(post)}
              pinning={pin.isPending && pin.variables?.id === p.id}
            />
          ))}

          <div ref={sentinelRef} className="h-4" />

          {q.hasNextPage && (
            <div className="flex justify-center pt-2">
              <Button
                type="button"
                variant="secondary"
                size="sm"
                onClick={() => q.fetchNextPage()}
                disabled={q.isFetchingNextPage}
              >
                {q.isFetchingNextPage ? "Loading…" : "Load more"}
              </Button>
            </div>
          )}

          {!q.hasNextPage && posts.length > 0 && (
            <div className="text-center text-[11px] text-fg-dim py-2">
              · end of feed ·
            </div>
          )}
        </div>
      )}

      {newPostOpen && (
        <PostComposer
          open={newPostOpen}
          onClose={() => setNewPostOpen(false)}
          onSuccess={() => q.refetch()}
        />
      )}

      {editingPost && accountId && (
        <EditPostModal
          post={editingPost}
          accountId={accountId}
          onClose={() => setEditingPost(null)}
          onSaved={(patch) =>
            patchCache(editingPost.id, (p) => ({
              ...p,
              rawText: patch.text,
              text: patch.text,
              price: patch.price,
            }))
          }
        />
      )}
    </div>
  );
}
