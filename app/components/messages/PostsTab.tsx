"use client";

/**
 * PostsTab — the model's own feed, rendered inside the Messages page.
 *
 * Unlike the PPV/Tips/All tabs (DB aggregates that span "All models"),
 * posts come live off OF per-account, so this tab owns a single-model
 * picker that defaults to the first active account. Infinite-scroll via
 * the same IntersectionObserver sentinel pattern as PaidMessagesTab.
 */

import { useEffect, useMemo, useRef, useState } from "react";

import { Button } from "@/components/ui/primitives";
import { PostCard } from "@/components/messages/PostCard";
import { useActiveAccounts } from "@/hooks/useAccounts";
import { usePosts } from "@/hooks/usePosts";

export default function PostsTab() {
  const accounts = useActiveAccounts();
  const [accountId, setAccountId] = useState<string | null>(null);

  // Default to the first active model once the list loads; respect an
  // explicit user pick afterwards.
  useEffect(() => {
    if (accountId == null && accounts.length > 0) setAccountId(accounts[0].id);
  }, [accounts, accountId]);

  const q = usePosts(accountId);
  const posts = useMemo(
    () => (q.data?.pages ?? []).flatMap((p) => p.posts),
    [q.data],
  );

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
        <Button
          type="button"
          variant="secondary"
          size="sm"
          onClick={() => q.refetch()}
          disabled={!accountId || q.isFetching}
          className="self-end border-border"
        >
          {q.isFetching ? "Loading…" : "Refresh"}
        </Button>
      </div>

      {q.isError && (
        <div className="p-4 text-sm text-err border border-err/30 bg-err/10 rounded-xl">
          Failed to load posts: {(q.error as Error)?.message || "unknown error"}
        </div>
      )}

      {!accountId ? (
        <div className="p-8 text-center text-sm text-fg-dim border border-border rounded-xl bg-panel">
          Pick a model to see their posts.
        </div>
      ) : q.isPending ? (
        <div className="p-8 text-center text-sm text-fg-dim">Loading…</div>
      ) : posts.length === 0 ? (
        <div className="p-8 text-center text-sm text-fg-dim border border-border rounded-xl bg-panel">
          No posts on this account yet.
        </div>
      ) : (
        <div className="space-y-3">
          {posts.map((p) => (
            <PostCard key={p.id} post={p} accountId={accountId} />
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
    </div>
  );
}
