"use client";

/**
 * VaultReviewTab — pending Vault-AI review items, grouped by kind.
 *
 * Contract: plans/VAULT_AI_ACTIONS_CONTRACT.md §2. Renders three sections
 * (Folders / PPVs / Reminders) with a per-SECTION Approve and a per-ITEM
 * Reject. Approve responses include a `stale` list — those ids stay pending
 * on the server and are badged "stale" here so the operator can re-review.
 *
 * Suggest-only posture: approve flips pending→approved on the ledger only.
 * Nothing hits OF until a downstream consumer applies. `approved ≠ applied`.
 */

import { useEffect, useMemo, useState } from "react";

import { AccountChips } from "@/components/automations/ReadyMadePanel";
import { useActiveAccounts } from "@/hooks/useAccounts";
import {
  type ReviewItem,
  type ReviewKind,
  useApproveIds,
  useApproveSection,
  useRejectIds,
  useVaultReview,
} from "@/hooks/useVaultReview";

interface SectionSpec {
  kind: ReviewKind;
  title: string;
  emptyLabel: string;
}

const SECTIONS: SectionSpec[] = [
  { kind: "folder", title: "Folders", emptyLabel: "No folder proposals waiting." },
  { kind: "ppv", title: "PPVs", emptyLabel: "No PPV drafts waiting." },
  { kind: "reminder", title: "Reminders", emptyLabel: "No reminder cards waiting." },
];

function cx(...parts: (string | false | null | undefined)[]): string {
  return parts.filter(Boolean).join(" ");
}

export default function VaultReviewTab() {
  const accounts = useActiveAccounts();
  const [accountId, setAccountId] = useState<string | null>(null);
  const [staleIds, setStaleIds] = useState<Set<number>>(new Set());
  const [flash, setFlash] = useState<string>("");

  useEffect(() => {
    if (!accountId && accounts.length > 0) setAccountId(accounts[0].id);
  }, [accounts, accountId]);

  const review = useVaultReview(accountId);
  const approveSection = useApproveSection(accountId);
  const approveIds = useApproveIds(accountId);
  const rejectIds = useRejectIds(accountId);

  const totals = useMemo(() => {
    const d = review.data;
    return {
      folder: d?.folder?.length ?? 0,
      ppv: d?.ppv?.length ?? 0,
      reminder: d?.reminder?.length ?? 0,
    };
  }, [review.data]);

  // Clear stale badges the moment the operator switches accounts — those ids
  // belong to a different ledger and would falsely flag cards that just happen
  // to share the numeric id.
  function onAccountChange(next: string | null) {
    setAccountId(next);
    setStaleIds(new Set());
    setFlash("");
  }

  async function onApproveSection(kind: ReviewKind) {
    if (!accountId) return;
    setFlash("");
    try {
      const r = await approveSection.mutateAsync(kind);
      const nextStale = new Set(staleIds);
      for (const id of r.stale) nextStale.add(id);
      setStaleIds(nextStale);
      setFlash(
        `Approved ${r.approved.length}${r.stale.length ? ` · ${r.stale.length} stale` : ""}`,
      );
    } catch (e) {
      setFlash(e instanceof Error ? e.message : "Approve failed");
    }
  }

  async function onReject(id: number) {
    if (!accountId) return;
    setFlash("");
    try {
      const r = await rejectIds.mutateAsync([id]);
      setStaleIds((prev) => {
        const n = new Set(prev);
        for (const rid of r.rejected) n.delete(rid);
        return n;
      });
      setFlash(`Rejected #${id}`);
    } catch (e) {
      setFlash(e instanceof Error ? e.message : "Reject failed");
    }
  }

  async function onApproveOne(id: number) {
    if (!accountId) return;
    setFlash("");
    try {
      const r = await approveIds.mutateAsync([id]);
      if (r.stale.includes(id)) {
        setStaleIds((prev) => new Set(prev).add(id));
        setFlash(`#${id} is stale`);
      } else {
        setStaleIds((prev) => {
          const n = new Set(prev);
          n.delete(id);
          return n;
        });
        setFlash(`Approved #${id}`);
      }
    } catch (e) {
      setFlash(e instanceof Error ? e.message : "Approve failed");
    }
  }

  if (accounts.length === 0) {
    return <div className="text-sm text-fg-dim">No model accounts with a live session.</div>;
  }

  const busy = approveSection.isPending || approveIds.isPending || rejectIds.isPending;

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-3 justify-between">
        <AccountChips accountId={accountId} onChange={onAccountChange} />
        <div className="text-xs text-fg-dim">
          {review.isLoading
            ? "Loading…"
            : `${totals.folder + totals.ppv + totals.reminder} pending`}
          {flash && <span className="ml-2 text-fg">{flash}</span>}
        </div>
      </div>

      {review.isError && (
        <div className="rounded-lg border border-red-500/40 bg-red-500/10 text-red-300 text-sm px-3 py-2">
          {review.error instanceof Error ? review.error.message : "Failed to load review queue."}
        </div>
      )}

      <div className="space-y-3">
        {SECTIONS.map((section) => {
          const items = (review.data?.[section.kind] ?? []) as ReviewItem[];
          const canApprove = items.length > 0 && !busy;
          return (
            <section
              key={section.kind}
              className="rounded-xl border border-border bg-bg-elev-1/40 overflow-hidden"
            >
              <header className="px-3 py-2 border-b border-border flex items-center justify-between gap-2">
                <div className="text-sm font-semibold uppercase tracking-wide">
                  {section.title}
                  <span className="ml-2 text-[11px] text-fg-dim font-normal normal-case">
                    {items.length} pending
                  </span>
                </div>
                <button
                  type="button"
                  onClick={() => onApproveSection(section.kind)}
                  disabled={!canApprove}
                  className="px-3 py-1 rounded-md text-xs border border-emerald-500/50 bg-emerald-500/10 text-emerald-400 hover:bg-emerald-500/20 disabled:opacity-40"
                >
                  Approve all
                </button>
              </header>

              {review.isLoading ? (
                <div className="px-3 py-4 text-xs text-fg-dim">Loading…</div>
              ) : items.length === 0 ? (
                <div className="px-3 py-4 text-xs text-fg-dim">{section.emptyLabel}</div>
              ) : (
                <ul className="divide-y divide-border/60">
                  {items.map((it) => (
                    <li key={it.id} className="px-3 py-2">
                      <ReviewCard
                        item={it}
                        stale={staleIds.has(it.id)}
                        busy={busy}
                        onApprove={() => onApproveOne(it.id)}
                        onReject={() => onReject(it.id)}
                      />
                    </li>
                  ))}
                </ul>
              )}
            </section>
          );
        })}
      </div>
    </div>
  );
}

function ReviewCard({
  item,
  stale,
  busy,
  onApprove,
  onReject,
}: {
  item: ReviewItem;
  stale: boolean;
  busy: boolean;
  onApprove: () => void;
  onReject: () => void;
}) {
  return (
    <div className="py-2.5 md:py-0 flex flex-col gap-2 md:flex-row md:items-start md:justify-between md:gap-3">
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2 text-xs text-fg-dim">
          <span>#{item.id}</span>
          <span>·</span>
          <span>{item.status}</span>
          {stale && (
            <span
              className="px-1.5 py-0.5 rounded bg-amber-500/15 text-amber-300 border border-amber-500/40 text-[10px] uppercase tracking-wide"
              title="Baseline changed underneath — re-review before approving."
            >
              stale
            </span>
          )}
        </div>
        <div className="mt-1">
          <PayloadView kind={item.kind} payload={item.payload} />
        </div>
      </div>
      <div className="flex items-center gap-2 md:gap-1 w-full md:w-auto shrink-0">
        <button
          type="button"
          onClick={onApprove}
          disabled={busy}
          className="flex-1 md:flex-initial inline-flex md:block items-center justify-center px-3 md:px-2 py-2 md:py-1 min-h-10 md:min-h-0 rounded-md text-sm md:text-xs border border-emerald-500/50 bg-emerald-500/10 text-emerald-400 hover:bg-emerald-500/20 disabled:opacity-40"
        >
          Approve
        </button>
        <button
          type="button"
          onClick={onReject}
          disabled={busy}
          className="flex-1 md:flex-initial inline-flex md:block items-center justify-center px-3 md:px-2 py-2 md:py-1 min-h-10 md:min-h-0 rounded-md text-sm md:text-xs border border-red-500/40 bg-red-500/10 text-red-300 hover:bg-red-500/20 disabled:opacity-40"
        >
          Reject
        </button>
      </div>
    </div>
  );
}

function PayloadView({ kind, payload }: { kind: ReviewKind; payload: Record<string, unknown> }) {
  if (kind === "folder") return <FolderPayload payload={payload} />;
  if (kind === "ppv") return <PpvPayload payload={payload} />;
  return <ReminderPayload payload={payload} />;
}

function readString(v: unknown): string | null {
  return typeof v === "string" && v ? v : null;
}
function readNumber(v: unknown): number | null {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}
function readIds(v: unknown): number[] {
  return Array.isArray(v) ? v.filter((x): x is number => typeof x === "number") : [];
}

function FolderPayload({ payload }: { payload: Record<string, unknown> }) {
  const name = readString(payload.folder_name) ?? readString(payload.folder_key) ?? "(unnamed)";
  const media = readIds(payload.media_ids);
  const rationale = readString(payload.rationale);
  return (
    <div className="text-sm">
      <div className="font-medium">📁 {name}</div>
      <div className="text-[11px] text-fg-dim mt-0.5">{media.length} media</div>
      {rationale && <div className="text-[11px] text-fg-dim mt-0.5">{rationale}</div>}
    </div>
  );
}

function PpvPayload({ payload }: { payload: Record<string, unknown> }) {
  const name = readString(payload.name) ?? "(unnamed)";
  const media = readIds(payload.media_ids);
  const previews = readIds(payload.preview_media_ids);
  const cents = readNumber(payload.price_cents);
  const tier = readString(payload.tier);
  const caption = readString(payload.caption);
  const script = readString(payload.script);
  return (
    <div className="text-sm">
      <div className="font-medium">💰 {name}</div>
      <div className="flex flex-wrap gap-x-3 gap-y-0.5 text-[11px] text-fg-dim mt-0.5">
        <span>{media.length} media</span>
        <span>{previews.length} preview</span>
        {typeof cents === "number" && <span>${(cents / 100).toFixed(2)}</span>}
        {tier && <span>tier: {tier}</span>}
      </div>
      {caption && <div className="text-[12px] text-fg mt-1">“{caption}”</div>}
      {script && <div className="text-[11px] text-fg-dim mt-0.5">script: {script}</div>}
    </div>
  );
}

function ReminderPayload({ payload }: { payload: Record<string, unknown> }) {
  const folder = readString(payload.folder_name) ?? "(no folder)";
  const media = readIds(payload.media_ids);
  const line = readString(payload.line);
  const audience = readString(payload.audience);
  const perDay = readNumber(payload.images_per_day);
  return (
    <div className="text-sm">
      <div className="font-medium">🔔 {folder}</div>
      <div className="flex flex-wrap gap-x-3 gap-y-0.5 text-[11px] text-fg-dim mt-0.5">
        <span>{media.length} media</span>
        {typeof perDay === "number" && <span>{perDay}/day</span>}
        {audience && <span>{audience}</span>}
      </div>
      {line && <div className="text-[12px] text-fg mt-1">“{line}”</div>}
    </div>
  );
}
