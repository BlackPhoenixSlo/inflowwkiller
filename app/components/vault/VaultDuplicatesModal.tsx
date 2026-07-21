"use client";

/**
 * VaultDuplicatesModal — review + confirm removal of re-uploaded vault media.
 *
 * A mature vault is ~30% re-uploads (measured on one account: 706 of 2,309). Each copy
 * costs a describe call, clutters the picker, and splits folder ordering.
 *
 * The relay groups them into sets where `original` is the EARLIEST-uploaded copy
 * and is always kept; everything else is a removal candidate, pre-selected. The
 * operator sees both sides as real thumbnails before anything happens.
 *
 * Removing calls OF's own "Remove selected items from vault"
 * (PUT /vault/media/hidden — the exact call OF's web app makes). The copies
 * leave her real vault but are NOT destroyed, so anything already attached to a
 * sent PPV or a live post keeps working. OF exposes no unhide, so this is
 * one-way — hence the explicit confirm step.
 *
 * The relay re-clusters server-side on submit and refuses any id that is an
 * original, is not currently a duplicate, or has been sent (unless allowed), so
 * a stale selection in this UI can never remove the keeper.
 */

import { useMemo, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";

import {
  hideDuplicates,
  mirrorThumbSrc,
  useDuplicates,
  type DupeCluster,
  type DupeMember,
  type HideDupesResult,
} from "@/hooks/useVaultCache";

const BAND_STYLE: Record<string, string> = {
  identical: "border-emerald-500/50 bg-emerald-500/10 text-emerald-400",
  near: "border-sky-500/50 bg-sky-500/10 text-sky-400",
  similar: "border-amber-500/50 bg-amber-500/10 text-amber-400",
  loose: "border-rose-500/50 bg-rose-500/10 text-rose-400",
};

const THRESHOLDS = [
  { v: 0, label: "0 · pixel-identical only" },
  { v: 2, label: "2 · recommended" },
  { v: 4, label: "4 · looser" },
  { v: 6, label: "6 · review every set" },
];

function fmtDate(iso: string | null): string {
  if (!iso) return "—";
  return iso.slice(0, 10);
}

function Tile({
  accountId,
  m,
  isOriginal,
  checked,
  onToggle,
}: {
  accountId: string;
  m: DupeMember;
  isOriginal: boolean;
  checked?: boolean;
  onToggle?: () => void;
}) {
  const band = m.band ?? "identical";
  return (
    <figure className="m-0 w-[126px] shrink-0">
      <button
        type="button"
        disabled={isOriginal}
        onClick={onToggle}
        className={[
          "relative block w-[126px] h-[126px] rounded-lg overflow-hidden border-2 transition-all",
          isOriginal
            ? "border-emerald-500 cursor-default"
            : checked
              ? "border-rose-500 opacity-100"
              : "border-border opacity-45 hover:opacity-75",
        ].join(" ")}
      >
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img
          src={mirrorThumbSrc(accountId, m.media_id)}
          alt={`media ${m.media_id}`}
          loading="lazy"
          className="w-full h-full object-cover"
        />
        {!isOriginal && (
          <span
            className={[
              "absolute top-1 right-1 w-5 h-5 rounded-md grid place-items-center text-[11px] font-bold",
              checked ? "bg-rose-500 text-white" : "bg-black/60 text-fg-dim",
            ].join(" ")}
          >
            {checked ? "✓" : ""}
          </span>
        )}
      </button>
      <figcaption className="mt-1 text-[10px] leading-tight text-fg-dim">
        <span className="block text-fg-dim/90">#{m.media_id}</span>
        {fmtDate(m.created_at)}
        {m.send_count > 0 && (
          <span className="block text-amber-400">sent {m.send_count}×</span>
        )}
        {isOriginal ? (
          <span className="mt-0.5 inline-block px-1.5 py-px rounded-full border border-emerald-500/50 bg-emerald-500/10 text-emerald-400 text-[9px] font-semibold">
            ORIGINAL · keep
          </span>
        ) : (
          <span
            className={`mt-0.5 inline-block px-1.5 py-px rounded-full border text-[9px] font-semibold ${
              BAND_STYLE[band] ?? BAND_STYLE.identical
            }`}
          >
            {m.exact ? "exact bytes" : `${band} d${m.dhash_dist}/a${m.ahash_dist}`}
          </span>
        )}
      </figcaption>
    </figure>
  );
}

export default function VaultDuplicatesModal({
  accountId,
  onClose,
}: {
  accountId: string;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const [threshold, setThreshold] = useState(2);
  const [deselected, setDeselected] = useState<Set<number>>(new Set());
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState<{ done: number; total: number } | null>(null);
  const [error, setError] = useState<string>("");
  const [result, setResult] = useState<HideDupesResult | null>(null);

  const scan = useDuplicates(accountId, threshold, true);
  const clusters: DupeCluster[] = useMemo(
    () => scan.data?.clusters ?? [],
    [scan.data],
  );

  // Everything is a candidate unless the operator explicitly unticked it, so
  // the default action is the safe-by-measurement one and un-checking is the
  // deliberate act.
  const selectedIds = useMemo(() => {
    const out: number[] = [];
    for (const c of clusters) {
      for (const d of c.dupes) if (!deselected.has(d.media_id)) out.push(d.media_id);
    }
    return out;
  }, [clusters, deselected]);

  const toggle = (id: number) =>
    setDeselected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const toggleSet = (c: DupeCluster) => {
    const ids = c.dupes.map((d) => d.media_id);
    const allOff = ids.every((i) => deselected.has(i));
    setDeselected((prev) => {
      const next = new Set(prev);
      for (const i of ids) {
        if (allOff) next.delete(i);
        else next.add(i);
      }
      return next;
    });
  };

  async function doHide() {
    setBusy(true);
    setError("");
    setProgress({ done: 0, total: selectedIds.length });
    try {
      const res = await hideDuplicates(accountId, selectedIds, {
        threshold,
        onProgress: (done, total) => setProgress({ done, total }),
      });
      setResult(res);
      setConfirming(false);
      if (res.of_error) setError(`OnlyFans rejected a batch: ${res.of_error}`);
      // The mirror rows are gone — refresh the grid, the counts and the scan.
      qc.invalidateQueries({ queryKey: ["vault-duplicates"] });
      qc.invalidateQueries({ queryKey: ["vault-mirror-items"] });
      qc.invalidateQueries({ queryKey: ["vault-cache-summary"] });
      qc.invalidateQueries({ queryKey: ["vault-of-folders-mirror"] });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      // eslint-disable-next-line no-console
      console.error("hide duplicates failed", e);
    } finally {
      setBusy(false);
      setProgress(null);
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 bg-black/50 md:grid md:place-items-center md:p-4"
      onClick={onClose}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="w-full h-full md:h-auto max-w-none md:max-w-[1100px] max-h-none md:max-h-[92vh] flex flex-col bg-panel border-0 md:border border-border rounded-none md:rounded-xl shadow-2xl"
      >
        <header className="px-4 py-3 border-b border-border flex items-start justify-between gap-3">
          <div className="min-w-0">
            <h2 className="text-sm font-semibold">Duplicate media</h2>
            <p className="text-[11px] text-fg-dim">
              Green = original (first uploaded, always kept). Everything ticked in
              red is removed from your OnlyFans vault.
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="px-2 py-2.5 md:py-1 min-h-[44px] md:min-h-0 rounded-md text-xs text-fg-dim hover:text-fg"
          >
            Close
          </button>
        </header>

        <div className="px-4 py-2.5 border-b border-border flex flex-wrap items-center gap-3 text-xs">
          <label className="flex items-center gap-1.5">
            <span className="text-fg-dim">Match strictness</span>
            <select
              value={threshold}
              onChange={(e) => {
                setThreshold(Number(e.target.value));
                setDeselected(new Set());
                setResult(null);
              }}
              className="px-2 py-1 rounded-md bg-bg-elev-1 border border-border"
            >
              {THRESHOLDS.map((t) => (
                <option key={t.v} value={t.v}>
                  {t.label}
                </option>
              ))}
            </select>
          </label>
          {scan.isFetching && <span className="text-fg-dim">scanning…</span>}
          {scan.data && (
            <>
              <span className="text-fg-dim">
                {scan.data.sets} sets · {scan.data.removable} copies ·{" "}
                {scan.data.scanned} items scanned
              </span>
              <span className="font-medium text-fg">
                {selectedIds.length} selected for removal
              </span>
            </>
          )}
        </div>

        {result && (
          <div className="mx-4 mt-3 px-3 py-2 rounded-lg border border-emerald-500/40 bg-emerald-500/10 text-xs text-emerald-300">
            Removed {result.hidden}{" "}
            {result.hidden === 1 ? "copy" : "copies"} from the OnlyFans vault.
            {Object.entries(result.refused).map(([why, ids]) => (
              <span key={why} className="block text-amber-400">
                refused ({why}): {ids.length}
              </span>
            ))}
            {!!result.not_hidden?.length && (
              <span className="block text-amber-400">
                {result.not_hidden.length} not removed — re-scan and try again.
              </span>
            )}
          </div>
        )}

        {error && (
          <div className="mx-4 mt-3 px-3 py-2 rounded-lg border border-rose-500/40 bg-rose-500/10 text-xs text-rose-300">
            {error}
          </div>
        )}

        <div className="flex-1 overflow-y-auto px-4 py-3 flex flex-col gap-3">
          {scan.isLoading && (
            <p className="text-xs text-fg-dim">Hashing thumbnails…</p>
          )}
          {scan.data && clusters.length === 0 && (
            <p className="text-xs text-fg-dim">No duplicates at this strictness.</p>
          )}
          {clusters.map((c) => {
            const ids = c.dupes.map((d) => d.media_id);
            const allOff = ids.every((i) => deselected.has(i));
            return (
              <section
                key={c.original.media_id}
                className="rounded-lg border border-border bg-bg-elev-1/40"
              >
                <div className="px-3 py-1.5 border-b border-border flex items-center gap-2 text-[11px]">
                  <b className="text-fg">
                    {c.dupes.length} duplicate{c.dupes.length === 1 ? "" : "s"}
                  </b>
                  <span
                    className={`px-1.5 py-px rounded-full border text-[9px] font-semibold ${
                      BAND_STYLE[c.band] ?? BAND_STYLE.identical
                    }`}
                  >
                    {c.band}
                  </span>
                  <span className="text-fg-dim">
                    {c.original.kind} · original uploaded{" "}
                    {fmtDate(c.original.created_at)}
                  </span>
                  {c.sent_dupes > 0 && (
                    <span className="text-amber-400">
                      {c.sent_dupes} already sent
                    </span>
                  )}
                  <button
                    type="button"
                    onClick={() => toggleSet(c)}
                    className="ml-auto px-2 py-0.5 rounded-md border border-border hover:text-fg text-fg-dim"
                  >
                    {allOff ? "Select set" : "Keep all"}
                  </button>
                </div>
                <div className="flex gap-2 overflow-x-auto px-3 py-3">
                  <Tile accountId={accountId} m={c.original} isOriginal />
                  <div className="w-px bg-border shrink-0 my-1" />
                  {c.dupes.map((d) => (
                    <Tile
                      key={d.media_id}
                      accountId={accountId}
                      m={d}
                      isOriginal={false}
                      checked={!deselected.has(d.media_id)}
                      onToggle={() => toggle(d.media_id)}
                    />
                  ))}
                </div>
              </section>
            );
          })}
        </div>

        <footer className="px-4 py-3 pb-[max(0.75rem,env(safe-area-inset-bottom))] md:pb-3 border-t border-border flex flex-wrap items-center gap-3">
          {!confirming ? (
            <button
              type="button"
              disabled={selectedIds.length === 0 || busy}
              onClick={() => setConfirming(true)}
              className="px-3 py-2.5 md:py-1.5 min-h-[44px] md:min-h-0 rounded-md text-xs font-medium border border-rose-500/50 bg-rose-500/10 text-rose-300 hover:bg-rose-500/20 disabled:opacity-40"
            >
              Remove {selectedIds.length} {selectedIds.length === 1 ? "copy" : "copies"} from
              OnlyFans…
            </button>
          ) : (
            <>
              <span className="text-xs text-rose-300">
                Removes {selectedIds.length} from your real vault. Originals are kept
                and sent PPVs keep working — but OnlyFans has no undo.
              </span>
              <button
                type="button"
                disabled={busy}
                onClick={doHide}
                className="px-3 py-2.5 md:py-1.5 min-h-[44px] md:min-h-0 rounded-md text-xs font-semibold border border-rose-500 bg-rose-500/20 text-rose-200 hover:bg-rose-500/30 disabled:opacity-40"
              >
                {busy
                  ? progress
                    ? `Removing ${progress.done}/${progress.total}…`
                    : "Removing…"
                  : "Yes, remove them"}
              </button>
              <button
                type="button"
                disabled={busy}
                onClick={() => setConfirming(false)}
                className="px-2 py-2.5 md:py-1 min-h-[44px] md:min-h-0 rounded-md text-xs text-fg-dim hover:text-fg"
              >
                Cancel
              </button>
            </>
          )}
          <span className="ml-auto text-[11px] text-fg-dim">
            Uses OnlyFans&rsquo; own “Remove from vault” — media is hidden, not deleted.
          </span>
        </footer>
      </div>
    </div>
  );
}
