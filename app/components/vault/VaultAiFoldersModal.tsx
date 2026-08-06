"use client";

/**
 * VaultAiFoldersModal — preview, then create, the AI-generated vault folders.
 *
 * Two kinds of folder come out of `service/vault_scripts.py`:
 *
 *   SCRIPTS — one shoot. Media grouped by the OUTFIT she is wearing (upload
 *     bursts are kind-pure, so grouping by upload splits a shoot's stills from
 *     its clips), ordered non-explicit → explicit. Sell these as a set.
 *   LANES — cut by heat and by how much is covered: Stories, Mass, Safe
 *     explicit, and the $10 / $50 tease tiers. An item may legitimately appear
 *     in several lanes and holds an INDEPENDENT position in each, which is why
 *     order lives on the folder membership rather than on the media.
 *
 * Ticking SOLO adds a `-solo` cut of each lane — `AI-tease 10-solo` is
 * `AI-tease 10` with everyone but her taken out. It adds rather than filters:
 * the two answer different questions, and an operator who wants both should not
 * have to rebuild the folders to switch between them.
 *
 * Nothing is written until the operator presses Create. The relay re-derives
 * the plan on apply rather than trusting what this preview posted, so a preview
 * left open while the vault changes underneath cannot write a stale grouping.
 * Every folder is prefixed `AI-`, re-running refreshes those folders instead of
 * duplicating them, and a folder the operator made by hand is never touched.
 *
 * No OF writes: these are internal folders. Nothing here sends anything.
 */

import { useEffect, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";

import {
  applyAiFolders,
  mirrorThumbSrc,
  runVaultFlags,
  useAiFolderPlan,
  useVaultFlagsStatus,
  type AiFolder,
  type ApplyAiFoldersResult,
} from "@/hooks/useVaultCache";

const TIER_STYLE: Record<string, string> = {
  sfw: "border-slate-500/50 bg-slate-500/10 text-slate-300",
  suggestive: "border-sky-500/50 bg-sky-500/10 text-sky-400",
  explicit: "border-amber-500/50 bg-amber-500/10 text-amber-400",
  hardcore: "border-rose-500/50 bg-rose-500/10 text-rose-400",
};

/** How many shoots stay their own folder. The rest still feed the lanes. */
const KEEP_CHOICES = [1, 2, 3, 4, 5];

function FolderCard({
  accountId,
  folder,
  multi,
}: {
  accountId: string;
  folder: AiFolder;
  multi: Set<number>;
}) {
  const [open, setOpen] = useState(false);
  const shown = open ? folder.items : folder.items.slice(0, 12);
  const isScript = folder.source === "script";

  return (
    <section className="rounded-lg border border-border bg-bg-elev-1">
      <header className="px-3 py-2 flex items-start justify-between gap-3 border-b border-border">
        <div className="min-w-0">
          <h3 className="text-xs font-semibold flex items-center gap-2">
            <span>{folder.solo ? "👤" : isScript ? "👗" : "📤"}</span>
            <span className="truncate">{folder.name}</span>
          </h3>
          <p className="text-[11px] text-fg-dim mt-0.5">
            {folder.purpose} · {folder.size} items ·{" "}
            {Object.entries(folder.kinds)
              .map(([k, n]) => `${n} ${k}`)
              .join(", ")}
            {isScript && ` · ${folder.closes_on_own} can close`}
            {/* "12 of 34" is the honest read: a solo cut that happens to be the
                whole lane is a real answer, and so is one that is a third of it. */}
            {folder.solo && ` · ${folder.size} of ${folder.solo_of} in the lane`}
          </p>
        </div>
        <div className="flex items-center gap-1 shrink-0">
          {Object.entries(folder.tiers).map(([tier, n]) => (
            <span
              key={tier}
              className={`px-1.5 py-0.5 rounded border text-[10px] ${
                TIER_STYLE[tier] ?? TIER_STYLE.sfw
              }`}
            >
              {tier} {n}
            </span>
          ))}
        </div>
      </header>

      <div className="px-3 py-2.5 flex flex-wrap gap-1.5">
        {shown.map((it) => (
          <figure key={it.media_id} className="m-0 w-[74px]">
            <div className="relative">
              <img
                src={mirrorThumbSrc(accountId, it.media_id)}
                alt=""
                loading="lazy"
                className="w-[74px] h-[74px] object-cover rounded border border-border"
              />
              <span className="absolute top-0 left-0 px-1 rounded-br bg-black/70 text-[10px] tabular-nums">
                {it.manual_order}
              </span>
              {multi.has(it.media_id) && (
                <span
                  title="also in another folder — it keeps a separate position in each"
                  className="absolute bottom-0 right-0 px-1 rounded-tl bg-black/70 text-[10px]"
                >
                  ⧉
                </span>
              )}
            </div>
            <figcaption className="mt-0.5 text-[10px] text-fg-dim truncate">
              {it.closes ? "closes" : it.clothing_state}
            </figcaption>
          </figure>
        ))}
      </div>

      {folder.items.length > 12 && (
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          className="w-full px-3 py-1.5 border-t border-border text-[11px] text-fg-dim hover:text-fg"
        >
          {open ? "Show fewer" : `Show all ${folder.items.length}`}
        </button>
      )}
    </section>
  );
}

export default function VaultAiFoldersModal({
  accountId,
  onClose,
}: {
  accountId: string;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const [keep, setKeep] = useState(2);
  const [mirrorToOf, setMirrorToOf] = useState(true);
  const [solo, setSolo] = useState(false);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<ApplyAiFoldersResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  const plan = useAiFolderPlan(accountId, { keep, solo }, true);

  // Items that land in more than one folder — the case the per-folder ordering
  // exists for. Flagged in the preview so the overlap is visible up front
  // rather than discovered later in the picker.
  const seen = new Map<number, number>();
  for (const f of plan.data?.folders ?? []) {
    for (const it of f.items) seen.set(it.media_id, (seen.get(it.media_id) ?? 0) + 1);
  }
  const multi = new Set([...seen.entries()].filter(([, n]) => n > 1).map(([m]) => m));

  async function onCreate() {
    setBusy(true);
    setError(null);
    try {
      // The SAME options the preview was built from: the server re-derives the
      // plan and retires generated folders it no longer makes, so applying
      // without `solo` would create the lanes and delete every `-solo` folder
      // in the same call.
      const res = await applyAiFolders(accountId, { keep, solo, mirrorToOf });
      setResult(res);
      // The folder rail and any per-folder grid are now stale.
      qc.invalidateQueries({ queryKey: ["vault-internal-folders", accountId] });
      qc.invalidateQueries({ queryKey: ["vault-of-folders-mirror", accountId] });
      qc.invalidateQueries({ queryKey: ["vault-lists", accountId] });
      qc.invalidateQueries({ queryKey: ["vault-mirror-items"] });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      console.error("apply AI folders failed", e);
    } finally {
      setBusy(false);
    }
  }

  const s = plan.data?.summary;
  const flags = plan.data?.flags;
  // Only when the box is ticked AND some described item was never asked who
  // else is in it — the one case where an empty solo cut is correct but reads
  // as breakage.
  const cov = solo ? plan.data?.solo_coverage : null;
  const soloGap = cov?.unknown ? cov : null;
  // Without the flags pass the paid tiers fall back to `clothing_state`, which
  // was measured wrong on ~1 in 3 of the stills the $50 tier is built from. The
  // folders would come out quietly wrong rather than visibly broken, so block
  // the button rather than warn beside it.
  const flagsMissing = !!flags && !flags.ready;

  // A whole-vault sweep runs for minutes in the background, so this watches it
  // rather than holding a request open. Polling starts on the click AND on open
  // when a sweep is already going (started from another tab, or before a reload).
  // `since` is when we last asked to watch. Comparing it against the query's
  // own timestamp is what stops the click from reading the PREVIOUS poll's
  // `running: false` and cancelling itself before the first real answer lands.
  const [since, setSince] = useState(() => Date.now());
  const flagsStatus = useVaultFlagsStatus(accountId, since > 0);
  const sweep = flagsStatus.data;
  const sweeping = !!sweep?.running;

  useEffect(() => {
    // Stop polling once it finishes, and re-plan on the fresh flags: the three
    // lanes that need them (safe explicit, $10, $50) were starved until now.
    if (since > 0 && flagsStatus.dataUpdatedAt > since && !sweep?.running) {
      setSince(0);
      plan.refetch();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sweep?.running, flagsStatus.dataUpdatedAt, since]);

  async function onRunFlags() {
    setError(null);
    try {
      await runVaultFlags(accountId);
      setSince(Date.now());
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  return (
    <div className="fixed inset-0 z-50 bg-black/50 md:grid md:place-items-center md:p-4" onClick={onClose}>
      <div
        onClick={(e) => e.stopPropagation()}
        className="w-full h-full md:h-auto max-w-none md:max-w-[1100px] max-h-none md:max-h-[92vh] flex flex-col bg-panel border-0 md:border border-border rounded-none md:rounded-xl shadow-2xl"
      >
        <header className="px-4 py-3 border-b border-border flex items-start justify-between gap-3">
          <div className="min-w-0">
            <h2 className="text-sm font-semibold">Build AI folders</h2>
            <p className="text-[11px] text-fg-dim">
              👗 scripts are one shoot, ordered tease → payoff. 📤 lanes are cut by
              heat and by how much is covered. Every folder is named{" "}
              <code className="text-fg">AI-</code> so you can tell them from your own.
            </p>
            {flagsMissing && (
              <div className="mt-2 rounded-md border border-amber-500/50 bg-amber-500/10 px-3 py-2">
                <p className="text-[11px] text-amber-300">
                  <b>Run the flags pass first.</b> {flags!.missing} of {flags!.stills}{" "}
                  stills have not been checked for what is actually on show, so the $10
                  and $50 tiers would fall back to <code>clothing_state</code> — measured
                  wrong on roughly 1 in 3 of the stills those tiers are built from. Takes
                  ~3s per still and costs about $0.001 per 100. Anything described from
                  now on is flagged in the same pass — this is a one-off catch-up on
                  media described before that.
                </p>
                {sweeping ? (
                  <p className="mt-2 text-[11px] text-amber-200 tabular-nums">
                    Checking… {sweep?.progress?.done ?? 0} of{" "}
                    {sweep?.progress?.total ?? flags!.missing} done
                    {!!sweep?.progress?.failed && ` · ${sweep.progress.failed} failed`}
                    {" · "}
                    <span className="text-amber-400/80">
                      keeps running if you close this
                    </span>
                  </p>
                ) : (
                  <button
                    type="button"
                    onClick={onRunFlags}
                    disabled={busy}
                    className="mt-2 px-2.5 py-1 rounded-md text-[11px] font-medium border border-amber-500/60 text-amber-200 hover:bg-amber-500/20 disabled:opacity-40"
                  >
                    Check {flags!.missing} stills
                  </button>
                )}
                {!sweeping && !!sweep?.progress?.error && (
                  <p className="mt-1 text-[11px] text-rose-300">
                    Last run stopped: {sweep.progress.error}
                  </p>
                )}
              </div>
            )}
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
            <span className="text-fg-dim">Keep as scripts</span>
            <select
              value={keep}
              onChange={(e) => {
                setKeep(Number(e.target.value));
                setResult(null);
              }}
              disabled={busy}
              className="px-2 py-1 rounded-md bg-bg-elev-1 border border-border"
            >
              {KEEP_CHOICES.map((n) => (
                <option key={n} value={n}>
                  top {n} {n === 1 ? "shoot" : "shoots"}
                </option>
              ))}
            </select>
          </label>
          <label className="flex items-center gap-1.5" title="Also create these as real folders on OnlyFans, so they show in the OF app and the chat picker">
            <input
              type="checkbox"
              checked={mirrorToOf}
              onChange={(e) => {
                setMirrorToOf(e.target.checked);
                setResult(null);
              }}
              disabled={busy}
            />
            <span className={mirrorToOf ? "text-amber-400" : "text-fg-dim"}>
              Also create on OnlyFans
            </span>
          </label>
          <label
            className="flex items-center gap-1.5"
            title="Also cut each lane down to the items nobody else is in — AI-tease 10-solo is AI-tease 10 with every partner clip and photo taken out. The lanes themselves are unchanged."
          >
            <input
              type="checkbox"
              checked={solo}
              onChange={(e) => {
                setSolo(e.target.checked);
                setResult(null);
              }}
              disabled={busy}
            />
            <span className={solo ? "text-violet-300" : "text-fg-dim"}>
              👤 Solo cut of each lane
            </span>
          </label>
          {plan.isFetching && <span className="text-fg-dim">planning…</span>}
          {s && (
            <span className="text-fg-dim">
              {s.folders} folders ({s.scripts} scripts, {s.lanes} lanes
              {s.solo_cuts > 0 && `, ${s.solo_cuts} solo`}) · {s.unique_media} media ·{" "}
              {s.memberships} placements
              {multi.size > 0 && ` · ⧉ ${multi.size} in more than one`}
            </span>
          )}
        </div>
        {/* An empty solo cut on a V1-described vault is CORRECT — `is_solo`
            refuses to call an item solo without the evidence — and reads as
            broken. Say which it is. */}
        {soloGap && (
          <div className="mx-4 mt-3 px-3 py-2 rounded-lg border border-violet-500/40 bg-violet-500/10 text-[11px] text-violet-200">
            <b>{soloGap.unknown} of {soloGap.described} described items were never
            asked who else is in them.</b>{" "}
            Only the rich (B) describe prompt records that, so those items stay OUT of the
            solo folders rather than being guessed into them — a partner clip in a solo
            folder is the wrong thing sold under the wrong promise. Re-scan them with
            prompt B to include them.
          </div>
        )}

        {result && (
          <div className="mx-4 mt-3 px-3 py-2 rounded-lg border border-emerald-500/40 bg-emerald-500/10 text-xs text-emerald-300">
            Created {result.new} new{" "}
            {result.reused > 0 && `and refreshed ${result.reused} existing `}
            {result.folders === 1 ? "folder" : "folders"} with {result.items} placements.
            {result.of_mirrored != null && (
              <span className="block text-emerald-400/80">
                {result.of_mirrored} mirrored to OnlyFans as real vault folders.
              </span>
            )}
            {!!result.of_failed && (
              <span className="block text-amber-400">
                {result.of_failed} could not be created on OnlyFans — the folder still
                exists here, press Rebuild to retry.
              </span>
            )}
            {result.of_mirrored == null && (
              <span className="block text-emerald-400/80">
                Internal folders only — nothing was written to OnlyFans.
              </span>
            )}
            <span className="block text-emerald-400/80">Nothing was sent.</span>
          </div>
        )}

        {error && (
          <div className="mx-4 mt-3 px-3 py-2 rounded-lg border border-rose-500/40 bg-rose-500/10 text-xs text-rose-300">
            {error}
          </div>
        )}

        <div className="flex-1 overflow-y-auto px-4 py-3 flex flex-col gap-3">
          {plan.isLoading && <p className="text-xs text-fg-dim">Reading the vault…</p>}
          {plan.isError && (
            <p className="text-xs text-rose-400">Could not build a plan for this account.</p>
          )}
          {plan.data && plan.data.folders.length === 0 && (
            <p className="text-xs text-fg-dim">
              Nothing to group yet — the vault needs to be collected and described first.
            </p>
          )}
          {plan.data?.folders.map((f) => (
            <FolderCard key={f.name} accountId={accountId} folder={f} multi={multi} />
          ))}
        </div>

        <footer className="px-4 py-3 pb-[max(0.75rem,env(safe-area-inset-bottom))] md:pb-3 border-t border-border flex items-center justify-between gap-3">
          <p className="text-[11px] text-fg-dim">
            {result
              ? "Re-running refreshes these folders rather than making duplicates."
              : mirrorToOf
                ? "Creates real folders on this OnlyFans account. Nothing is sent."
                : "Nothing has been created yet. Your own folders are never touched."}
          </p>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={onClose}
              className="px-3 py-2.5 md:py-1.5 min-h-[44px] md:min-h-0 rounded-md text-xs border border-border hover:bg-bg-elev-1"
            >
              {result ? "Done" : "Cancel"}
            </button>
            <button
              type="button"
              onClick={onCreate}
              disabled={busy || flagsMissing || !plan.data?.folders.length}
              title={
                flagsMissing
                  ? "Run the flags pass first — the $10/$50 tiers would be built on an unreliable signal"
                  : undefined
              }
              className="px-3 py-2.5 md:py-1.5 min-h-[44px] md:min-h-0 rounded-md text-xs font-medium bg-accent text-black disabled:opacity-40"
            >
              {busy
                ? "Creating…"
                : result
                  ? "Rebuild"
                  : `Create ${s?.folders ?? 0} folders`}
            </button>
          </div>
        </footer>
      </div>
    </div>
  );
}
