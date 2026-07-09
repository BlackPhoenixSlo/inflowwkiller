"use client";

/**
 * MassMessagesTab — past mass-message broadcasts, per account.
 *
 * Reads broadcasts from the relay's local cache + offers bulk-cancel
 * actions. Unlike per-chat unsend, mass-message unsend has no edit
 * window — OF reports `canUnsend: true, unsendSeconds: 1000000` on
 * these rows long after the 24h chat window has closed.
 *
 * Bulk actions run client-side over the currently-visible rows; each
 * row's cancel mutation already write-throughs the cache + invalidates
 * the read query, so the UI converges without manual refetch logic.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { Lock, Paperclip } from "lucide-react";

import { Button, Card } from "@/components/ui/primitives";
import { useActiveAccounts } from "@/hooks/useAccounts";
import {
  useCancelMassMessage,
  useMassMessages,
  useRefreshMassMessages,
  type MassMessageRow,
} from "@/hooks/useMassMessages";
import {
  useAutomationRules,
  useCreateRule,
  useUpdateRule,
} from "@/hooks/useAutomations";
import { proxyImage } from "@/lib/relay";
import { cn } from "@/lib/utils";

/** Name + cadence of the scheduled auto-unsend rule this tab manages (one
 *  `unsend_messages` automation_rules row per account, keyed by this name). */
const AUTO_RULE_NAME = "Auto-unsend mass";
const AUTO_EVERY_SECONDS = 3600; // hourly sweep

const RANGE_OPTIONS = [
  { label: "Last 7 days", days: 7 },
  { label: "Last 30 days", days: 30 },
  { label: "Last 90 days", days: 90 },
  { label: "Last 365 days", days: 365 },
];

/** True when the row carries non-GIF visual media (photos / videos).
 *  GIFs live under `giphyId`, not `media[]`, so GIF-only rows return
 *  false here. Used by the bulk-action filters. */
function hasImageMedia(m: MassMessageRow): boolean {
  return (m.mediaCount ?? 0) > 0;
}

/** Age gate for every TYPED bulk action (non-image / with-images / free text) —
 *  mirrors the backend unsend_messages `policy.mass_text_hours` (a blast has
 *  done its job after this long). "Unsend all" ignores it on purpose so
 *  fresh sends can still be pulled. */
const BULK_MIN_AGE_H = 4;

function olderThanHours(m: MassMessageRow, hours: number): boolean {
  if (!m.date) return false; // unknown sent time → never auto-eligible
  const t = new Date(m.date).getTime();
  if (Number.isNaN(t)) return false;
  return (Date.now() - t) / 3_600_000 >= hours;
}

/** Free (no price) + image-less + older than the threshold — the exact set the
 *  backend `policy.mass_text_hours` sweep targets. GIF-only counts as image-less. */
function isFreeTextOld(m: MassMessageRow): boolean {
  return !!m.isFree && !hasImageMedia(m) && olderThanHours(m, BULK_MIN_AGE_H);
}

/** Concurrency cap for bulk cancels. Each cancel is one OF request +
 *  one DB update — small enough to run a few in parallel, large
 *  enough to be polite. */
const BULK_CHUNK = 4;

export default function MassMessagesTab() {
  const accounts = useActiveAccounts();
  const accountIds = useMemo(() => accounts.map((a) => a.id), [accounts]);
  const [daysBack, setDaysBack] = useState(30);
  const [hideUnsent, setHideUnsent] = useState(true);
  // Multi-select model filter. Empty set = no filter (show all). Otherwise
  // restrict to the accounts in the set. Bulk actions inherit this scope.
  const [selectedModels, setSelectedModels] = useState<Set<string>>(new Set());
  const [bulkRunning, setBulkRunning] = useState<string | null>(null);

  const { rows, isLoading, isFetching, error } = useMassMessages(accountIds, { daysBack });
  const cancelM = useCancelMassMessage();
  const refreshM = useRefreshMassMessages();

  const refresh = () => {
    if (accountIds.length === 0) return;
    refreshM.mutate({ accountIds, daysBack });
  };

  // First-mount auto-warm: if the cache is empty for this account/range,
  // fire a refresh so the user doesn't sit on an empty list.
  const autoRefreshedRef = useRef<Set<string>>(new Set());
  useEffect(() => {
    if (isLoading || isFetching || refreshM.isPending) return;
    if (accountIds.length === 0) return;
    if (rows.length > 0) return;
    const key = `${daysBack}|${accountIds.join(",")}`;
    if (autoRefreshedRef.current.has(key)) return;
    autoRefreshedRef.current.add(key);
    refreshM.mutate({ accountIds, daysBack });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [accountIds.join(","), daysBack, isLoading, isFetching]);

  // Drop ids from the filter set when the account is no longer active so
  // we don't render an empty list filtered by a model that's gone.
  useEffect(() => {
    setSelectedModels((prev) => {
      if (prev.size === 0) return prev;
      const live = prev.size > 0 ? new Set([...prev].filter((id) => accountIds.includes(id))) : prev;
      return live.size === prev.size ? prev : live;
    });
  }, [accountIds]);

  const refreshing = isFetching || refreshM.isPending;

  const accountName = (id?: string) =>
    accounts.find((a) => a.id === id)?.nickname || id || "?";

  // Filter visible rows by selected models. Empty set = show everything.
  // Bulk actions operate on this slice, so the model bubbles also scope
  // the "Unsend all" buttons.
  const visibleRows = useMemo(() => {
    let out = rows;
    if (selectedModels.size > 0)
      out = out.filter((r) => r.__accountId && selectedModels.has(r.__accountId));
    if (hideUnsent) out = out.filter((r) => !r.isCanceled);
    return out;
  }, [rows, selectedModels, hideUnsent]);

  const actionableRows = useMemo(
    () => visibleRows.filter((r) => r.canUnsend && !r.isCanceled && r.__accountId),
    [visibleRows],
  );

  /** Run cancel mutations across a target subset of rows, in small
   *  parallel batches, with one combined confirmation up front. */
  async function bulkCancel(
    actionKey: string,
    label: string,
    predicate: (r: MassMessageRow) => boolean,
  ) {
    const targets = actionableRows.filter(predicate);
    if (targets.length === 0) {
      window.alert("Nothing to unsend matches that filter.");
      return;
    }
    const ok = window.confirm(
      `${label}\n\nUnsend ${targets.length} broadcast${targets.length === 1 ? "" : "s"} from EVERY recipient? This can't be undone.`,
    );
    if (!ok) return;
    setBulkRunning(actionKey);
    let failed = 0;
    try {
      for (let i = 0; i < targets.length; i += BULK_CHUNK) {
        const chunk = targets.slice(i, i + BULK_CHUNK);
        const results = await Promise.allSettled(
          chunk.map((r) =>
            cancelM.mutateAsync({ id: r.id, accountId: r.__accountId! }),
          ),
        );
        failed += results.filter((res) => res.status === "rejected").length;
      }
    } finally {
      setBulkRunning(null);
    }
    if (failed > 0) {
      window.alert(
        `${failed} of ${targets.length} unsend${targets.length === 1 ? "" : "s"} failed. Those broadcasts may still be live — refresh and retry.`,
      );
    }
  }

  // Counters for the button labels — shows up-front whether each action
  // will do anything before the user clicks.
  const counts = useMemo(() => {
    const all = actionableRows.length;
    const nonImage = actionableRows.filter(
      (r) => !hasImageMedia(r) && olderThanHours(r, BULK_MIN_AGE_H),
    ).length;
    const withImage = actionableRows.filter(
      (r) => hasImageMedia(r) && olderThanHours(r, BULK_MIN_AGE_H),
    ).length;
    const freeTextOld = actionableRows.filter(isFreeTextOld).length;
    return { all, nonImage, withImage, freeTextOld };
  }, [actionableRows]);

  return (
    <div className="space-y-4">
      {/* Auto-unsend automation — a scheduled `unsend_messages` rule per model.
       *  Hourly sweep that unsends broadcasts older than the set hours, by type
       *  (free text / image / PPV). Leave a type off to keep it forever. */}
      <Card>
        <div className="mb-3">
          <h2 className="text-base font-semibold">Auto-unsend (automation)</h2>
          <p className="text-xs text-fg-dim mt-0.5">
            Runs hourly per model. Automatically unsends broadcasts older than the
            chosen hours, by type — leave a type unchecked to keep those forever.
          </p>
        </div>
        <div className="space-y-2.5">
          {accounts.length === 0 && (
            <div className="text-xs text-fg-dim italic">No active models.</div>
          )}
          {accounts.map((a) => (
            <AutoUnsendPanel key={a.id} accountId={a.id} label={a.nickname || a.id} />
          ))}
        </div>
      </Card>

      <Card>
        <div className="flex items-center justify-between gap-3 mb-3 flex-wrap">
          <div>
            <h2 className="text-base font-semibold">Mass messages</h2>
            <p className="text-xs text-fg-dim mt-0.5">
              Broadcasts already sent. Cancel one to unsend it from EVERY recipient — no 24h edit window for mass sends.
            </p>
          </div>
          <div className="flex items-center gap-2 flex-wrap">
            <label className="flex items-center gap-1.5 text-xs text-fg-dim cursor-pointer select-none">
              <input
                type="checkbox"
                checked={hideUnsent}
                onChange={(e) => setHideUnsent(e.target.checked)}
              />
              Hide unsent
            </label>
            <select
              className="text-xs bg-bg-elev-1 border border-border rounded px-2 py-1"
              value={daysBack}
              onChange={(e) => setDaysBack(Number(e.target.value))}
            >
              {RANGE_OPTIONS.map((o) => (
                <option key={o.days} value={o.days}>{o.label}</option>
              ))}
            </select>
            <Button size="sm" variant="secondary" onClick={refresh} disabled={refreshing}>
              <span className={cn("inline-block mr-1", refreshing && "animate-spin")}>↻</span>
              {refreshing ? "Refreshing…" : "Refresh"}
            </Button>
          </div>
        </div>

        {accounts.length > 1 && (
          <div className="flex items-center gap-1.5 flex-wrap mb-3">
            <span className="text-[10px] uppercase tracking-wide text-fg-dim mr-1">Models</span>
            <button
              type="button"
              onClick={() => setSelectedModels(new Set())}
              className={cn(
                "px-2.5 py-1 rounded-full text-xs border transition-colors",
                selectedModels.size === 0
                  ? "bg-accent text-white border-accent"
                  : "bg-bg-elev-1 text-fg-dim border-border hover:text-fg hover:border-fg-dim",
              )}
            >
              All ({accounts.length})
            </button>
            {accounts.map((a) => {
              const active = selectedModels.has(a.id);
              return (
                <button
                  key={a.id}
                  type="button"
                  onClick={() =>
                    setSelectedModels((prev) => {
                      const next = new Set(prev);
                      if (next.has(a.id)) next.delete(a.id);
                      else next.add(a.id);
                      return next;
                    })
                  }
                  className={cn(
                    "px-2.5 py-1 rounded-full text-xs border transition-colors",
                    active
                      ? "bg-accent text-white border-accent"
                      : "bg-bg-elev-1 text-fg-dim border-border hover:text-fg hover:border-fg-dim",
                  )}
                >
                  {a.nickname || a.id}
                </button>
              );
            })}
          </div>
        )}

        {/* Bulk action bar. Disabled when nothing matches so the user
         *  doesn't fire a confirmation just to be told "nothing to do". */}
        <div className="flex items-center gap-2 flex-wrap mb-3 pb-3 border-b border-border">
          <span className="text-[11px] text-fg-dim mr-1">
            Bulk actions ({actionableRows.length} unsendable in view):
          </span>
          <Button
            size="sm"
            variant="danger"
            disabled={!!bulkRunning || counts.all === 0}
            onClick={() => bulkCancel("all", "Unsend ALL visible broadcasts", () => true)}
          >
            {bulkRunning === "all" ? "Unsending…" : `Unsend all (${counts.all})`}
          </Button>
          <Button
            size="sm"
            variant="danger"
            disabled={!!bulkRunning || counts.nonImage === 0}
            onClick={() =>
              bulkCancel(
                "non-image",
                `Unsend non-image broadcasts (text + GIF + empty) older than ${BULK_MIN_AGE_H}h`,
                (r) => !hasImageMedia(r) && olderThanHours(r, BULK_MIN_AGE_H),
              )
            }
          >
            {bulkRunning === "non-image"
              ? "Unsending…"
              : `Unsend non-image (GIFs incl.) >${BULK_MIN_AGE_H}h (${counts.nonImage})`}
          </Button>
          <Button
            size="sm"
            variant="danger"
            disabled={!!bulkRunning || counts.withImage === 0}
            onClick={() =>
              bulkCancel(
                "with-image",
                `Unsend broadcasts that include images / videos, older than ${BULK_MIN_AGE_H}h`,
                (r) => hasImageMedia(r) && olderThanHours(r, BULK_MIN_AGE_H),
              )
            }
          >
            {bulkRunning === "with-image"
              ? "Unsending…"
              : `Unsend with images >${BULK_MIN_AGE_H}h (${counts.withImage})`}
          </Button>
          {/* The free-text cleanup — same target set as the backend
           *  unsend_messages `policy.mass_text_hours` sweep: free (no price),
           *  image-less, past the age gate. Keeps revenue (PPV/image) blasts. */}
          <Button
            size="sm"
            variant="danger"
            disabled={!!bulkRunning || counts.freeTextOld === 0}
            onClick={() =>
              bulkCancel(
                "free-text-old",
                `Unsend FREE text-only broadcasts older than ${BULK_MIN_AGE_H}h`,
                isFreeTextOld,
              )
            }
            title={`Free (no price), no image, older than ${BULK_MIN_AGE_H}h — leaves PPV/image blasts alone`}
          >
            {bulkRunning === "free-text-old"
              ? "Unsending…"
              : `Unsend free text >${BULK_MIN_AGE_H}h (${counts.freeTextOld})`}
          </Button>
        </div>

        {error && (
          <div className="text-sm text-err mb-3">
            Failed to load: {error.message}
          </div>
        )}
        {refreshM.error && (
          <div className="text-sm text-err mb-3">
            Refresh failed: {refreshM.error.message}
          </div>
        )}

        {isLoading && visibleRows.length === 0 && (
          <div className="text-sm text-fg-dim">Loading…</div>
        )}

        {!isLoading && visibleRows.length === 0 && !refreshing && (
          <div className="text-xs text-fg-dim italic">
            {selectedModels.size === 0
              ? "No mass messages cached for this range. Click Refresh to fetch from OF."
              : `No mass messages for ${[...selectedModels].map(accountName).join(", ")} in this range.`}
          </div>
        )}
        {!isLoading && visibleRows.length === 0 && refreshing && (
          <div className="text-xs text-fg-dim italic">Fetching from OF…</div>
        )}

        <div className="space-y-2">
          {visibleRows.map((m) => (
            <MassMessageRowItem
              key={`${m.__accountId}:${m.id}`}
              m={m}
              accountLabel={accountName(m.__accountId)}
              busy={
                !!bulkRunning ||
                (cancelM.isPending && cancelM.variables?.id === m.id)
              }
              onCancel={() => {
                if (!m.__accountId) return;
                const recip = m.sentCount ?? 0;
                const msg = recip
                  ? `Unsend this message from ALL ${recip} recipient${recip === 1 ? "" : "s"}? This can't be undone.`
                  : "Unsend this mass message from every recipient? This can't be undone.";
                if (!confirm(msg)) return;
                cancelM.mutate({ id: m.id, accountId: m.__accountId });
              }}
            />
          ))}
        </div>
      </Card>
    </div>
  );
}

/** Per-model auto-unsend config. Reads/writes the single `unsend_messages` rule
 *  named AUTO_RULE_NAME for this account; the payload carries
 *  `policy.{mass_text_hours,mass_media_hours,mass_price_hours}` for the enabled
 *  classes (mirrors the backend sweep). */
function AutoUnsendPanel({ accountId, label }: { accountId: string; label: string }) {
  const rulesQ = useAutomationRules(accountId);
  const createM = useCreateRule(accountId);
  const updateM = useUpdateRule(accountId);
  const existing = useMemo(
    () =>
      (rulesQ.data ?? []).find(
        (r) => r.kind === "unsend_messages" && r.name === AUTO_RULE_NAME,
      ),
    [rulesQ.data],
  );

  const [enabled, setEnabled] = useState(false);
  const [text, setText] = useState({ on: true, h: 4 });
  const [img, setImg] = useState({ on: false, h: 24 });
  const [price, setPrice] = useState({ on: false, h: 48 });

  // Hydrate the form once from the saved rule when it first loads.
  const hydratedRef = useRef(false);
  useEffect(() => {
    if (hydratedRef.current || !existing) return;
    hydratedRef.current = true;
    setEnabled(existing.is_enabled);
    const p = (existing.payload?.policy ?? {}) as Record<string, number>;
    setText(p.mass_text_hours != null ? { on: true, h: p.mass_text_hours } : { on: false, h: 4 });
    setImg(p.mass_media_hours != null ? { on: true, h: p.mass_media_hours } : { on: false, h: 24 });
    setPrice(p.mass_price_hours != null ? { on: true, h: p.mass_price_hours } : { on: false, h: 48 });
  }, [existing]);

  const saving = createM.isPending || updateM.isPending;
  const err = createM.error || updateM.error;

  const save = () => {
    const policy: Record<string, number> = {};
    if (text.on) policy.mass_text_hours = text.h;
    if (img.on) policy.mass_media_hours = img.h;
    if (price.on) policy.mass_price_hours = price.h;
    const payload = { policy };
    if (existing) {
      updateM.mutate({
        id: existing.id,
        is_enabled: enabled,
        every_seconds: AUTO_EVERY_SECONDS,
        payload,
      });
    } else {
      createM.mutate({
        account_id: accountId,
        kind: "unsend_messages",
        name: AUTO_RULE_NAME,
        every_seconds: AUTO_EVERY_SECONDS,
        payload,
        is_enabled: enabled,
      });
    }
  };

  return (
    <div className="border border-border rounded-md p-3 space-y-2">
      <div className="flex items-center justify-between gap-2">
        <label className="flex items-center gap-2 text-sm font-medium cursor-pointer">
          <input
            type="checkbox"
            checked={enabled}
            onChange={(e) => setEnabled(e.target.checked)}
          />
          {label}
          {existing?.is_enabled && <span className="text-[10px] text-accent">● active</span>}
        </label>
        <span className="text-[10px] text-fg-dim">runs hourly</span>
      </div>
      <AutoClassRow label="Free text-only" v={text} set={setText} />
      <AutoClassRow label="With image / video" v={img} set={setImg} />
      <AutoClassRow label="PPV (priced)" v={price} set={setPrice} />
      <div className="flex items-center gap-2 pt-1 flex-wrap">
        <Button size="sm" variant="secondary" onClick={save} disabled={saving}>
          {saving ? "Saving…" : existing ? "Update" : "Save"}
        </Button>
        {err && <span className="text-xs text-err">{err.message}</span>}
        {existing?.last_run && (
          <span className="text-[10px] text-fg-dim">
            last run: {existing.last_run.status}
            {existing.next_due_at
              ? ` · next ${new Date(existing.next_due_at).toLocaleTimeString()}`
              : ""}
          </span>
        )}
      </div>
    </div>
  );
}

function AutoClassRow({
  label, v, set,
}: {
  label: string;
  v: { on: boolean; h: number };
  set: (x: { on: boolean; h: number }) => void;
}) {
  return (
    <div className="flex items-center gap-2 text-xs">
      <label className="flex items-center gap-1.5 w-36 cursor-pointer">
        <input
          type="checkbox"
          checked={v.on}
          onChange={(e) => set({ ...v, on: e.target.checked })}
        />
        {label}
      </label>
      <span className="text-fg-dim">after</span>
      <input
        type="number"
        min={1}
        className="w-16 bg-bg-elev-1 border border-border rounded px-1.5 py-0.5 disabled:opacity-40"
        value={v.h}
        disabled={!v.on}
        onChange={(e) => set({ ...v, h: Math.max(1, Number(e.target.value) || 1) })}
      />
      <span className="text-fg-dim">hours</span>
    </div>
  );
}

// Friendly labels for the automation_kind tokens that produce broadcasts.
const MASS_AUTOMATION_LABELS: Record<string, string> = {
  send_mass_message: "Mass message",
  reply_mass_funnel: "Funnel reply",
  mass_nudge: "Mass nudge",
  online_blast: "Online blast",
  ppv_send: "PPV Library",
};

function MassMessageRowItem({
  m, accountLabel, busy, onCancel,
}: {
  m: MassMessageRow;
  accountLabel: string;
  busy: boolean;
  onCancel: () => void;
}) {
  const when = m.date ? new Date(m.date) : null;
  const sent = m.sentCount ?? 0;
  const viewed = m.viewedCount ?? 0;
  const canUnsend = !!m.canUnsend && !m.isCanceled;
  const firstMedia = m.media?.[0];
  const thumbUrl =
    firstMedia?.files?.squarePreview?.url ||
    firstMedia?.files?.thumb?.url ||
    firstMedia?.files?.preview?.url ||
    null;

  return (
    <div className={cn(
      "border border-border rounded-md px-3 py-2.5 flex items-start gap-3",
      busy && "opacity-60",
      m.isCanceled && "opacity-50",
    )}>
      {thumbUrl && (
        <div className="shrink-0">
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img
            src={proxyImage(thumbUrl, m.__accountId)}
            alt=""
            className="w-12 h-12 rounded object-cover border border-border"
          />
        </div>
      )}
      <div className="flex-1 min-w-0 space-y-1">
        <div className="flex items-center gap-2 text-[11px] flex-wrap">
          <span className="px-1.5 py-0.5 rounded bg-accent/15 text-accent">
            {when ? when.toLocaleString() : "no date"}
          </span>
          <span className="text-fg-dim">acct {accountLabel}</span>
          {m.automationKind && (
            <span
              className="px-1.5 py-0.5 rounded bg-warn/15 text-warn"
              title="Sent by an automation (joined via mass_runs)"
            >
              🤖 {MASS_AUTOMATION_LABELS[m.automationKind] ?? m.automationKind}
              {m.funnelName ? ` · ${m.funnelName}` : ""}
            </span>
          )}
          <span className="text-fg-dim">📢 {sent} sent · {viewed} viewed</span>
          {(m.mediaCount ?? 0) > 0 && (
            <span className="text-fg-dim inline-flex items-center gap-1">
              <Paperclip size={12} aria-hidden /> {m.mediaCount}
            </span>
          )}
          {m.giphyId && <span className="text-fg-dim">🎞 GIF</span>}
          {!m.isFree && (
            <span className="text-warn inline-flex items-center gap-1">
              <Lock size={12} aria-hidden /> PPV
            </span>
          )}
          {m.isCanceled && (
            <span className="px-1.5 py-0.5 rounded bg-err/15 text-err">unsent</span>
          )}
        </div>
        <div className="text-sm whitespace-pre-wrap break-words line-clamp-3">
          {stripHtml(m.text || m.rawText || "")}
          {!m.text && !m.rawText && m.giphyId && (
            <span className="text-fg-dim italic">(GIF only)</span>
          )}
        </div>
      </div>
      <Button
        size="sm"
        variant="danger"
        onClick={onCancel}
        disabled={busy || !canUnsend}
        title={
          m.isCanceled
            ? "Already unsent"
            : canUnsend
            ? "Unsend from every recipient"
            : "OF marked this as no-longer-unsendable"
        }
      >
        {m.isCanceled ? "Unsent" : "Unsend this"}
      </Button>
    </div>
  );
}

function stripHtml(s: string): string {
  return s
    .replace(/<br\s*\/?>/gi, "\n")
    .replace(/<\/p\s*>/gi, "\n")
    .replace(/<[^>]+>/g, "")
    .replace(/&nbsp;/g, " ")
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, "\"")
    .replace(/&#39;/g, "'")
    .trim();
}
