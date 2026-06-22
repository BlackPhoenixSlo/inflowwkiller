"use client";

/**
 * TopPostsTab — best-performing posts in the selected window.
 *
 * This is the only place purchases/earnings ranking is available: OF's
 * feed payload carries no per-post buy counts, but /posts/top returns the
 * posts already sorted by the chosen metric. Order = rank, which we surface
 * as a "#N" chip on each card. Uses the page-level date range.
 */

import { useEffect, useMemo, useState } from "react";

import { Button } from "@/components/ui/primitives";
import { PostCard } from "@/components/messages/PostCard";
import { useActiveAccounts } from "@/hooks/useAccounts";
import { useTopPosts, type TopBy } from "@/hooks/usePosts";
import { cn } from "@/lib/utils";

interface Props {
  from: string | null;
  to: string | null;
}

const METRICS: { key: TopBy; label: string }[] = [
  { key: "purchases", label: "Purchases" },
  { key: "likes", label: "Likes" },
  { key: "comments", label: "Comments" },
  { key: "views", label: "Views" },
];

export default function TopPostsTab({ from, to }: Props) {
  const accounts = useActiveAccounts();
  const [accountId, setAccountId] = useState<string | null>(null);
  const [by, setBy] = useState<TopBy>("purchases");

  useEffect(() => {
    if (accountId == null && accounts.length > 0) setAccountId(accounts[0].id);
  }, [accounts, accountId]);

  const q = useTopPosts(accountId, by, from, to);
  const posts = useMemo(() => q.data ?? [], [q.data]);

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-3 p-3 bg-panel border border-border rounded-xl">
        <label className="flex flex-col gap-1">
          <span className="text-[10px] uppercase tracking-wide text-fg-dim">Model</span>
          <select
            value={accountId ?? ""}
            onChange={(e) => setAccountId(e.target.value || null)}
            className="bg-bg border border-border rounded-lg px-2 py-1.5 text-xs text-fg focus:outline-none focus:border-accent"
          >
            {accounts.length === 0 && <option value="">No models</option>}
            {accounts.map((a) => (
              <option key={a.id} value={a.id}>
                {a.nickname || a.id}
              </option>
            ))}
          </select>
        </label>

        <label className="flex flex-col gap-1">
          <span className="text-[10px] uppercase tracking-wide text-fg-dim">Rank by</span>
          <div className="flex items-center gap-1">
            {METRICS.map((m) => (
              <Button
                key={m.key}
                type="button"
                variant={by === m.key ? "primary" : "secondary"}
                size="sm"
                onClick={() => setBy(m.key)}
                className={cn(by !== m.key && "border-border")}
              >
                {m.label}
              </Button>
            ))}
          </div>
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
          Failed to load top posts: {(q.error as Error)?.message || "unknown error"}
        </div>
      )}

      {!accountId ? (
        <div className="p-8 text-center text-sm text-fg-dim border border-border rounded-xl bg-panel">
          Pick a model to see top posts.
        </div>
      ) : q.isPending ? (
        <div className="p-8 text-center text-sm text-fg-dim">Loading…</div>
      ) : posts.length === 0 ? (
        <div className="p-8 text-center text-sm text-fg-dim border border-border rounded-xl bg-panel">
          No posts in this window.
        </div>
      ) : (
        <div className="space-y-3">
          {posts.map((p, i) => (
            <PostCard key={p.id} post={p} accountId={accountId} rank={i + 1} />
          ))}
        </div>
      )}
    </div>
  );
}
