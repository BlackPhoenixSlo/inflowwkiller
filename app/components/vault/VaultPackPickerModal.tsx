"use client";

/**
 * VaultPackPickerModal — one button, every candidate, one Save.
 *
 * The operator opens it, sees every photo in the vault that might be a feet
 * photo, files each one on a rung (or rejects it), and presses Save. That
 * writes the rung folders and their membership. Publishing them to her real
 * OnlyFans vault is a separate, explicit button per rung.
 *
 * WHY A HUMAN IS IN THIS LOOP AT ALL. The vault's own taxonomy cannot find this
 * content. Measured on prod 2026-08-10: `primary_folder='feet'` is set on 265
 * items roster-wide but ZERO on either pilot account, and zero of the 68 items
 * the operator hand-picked on 08-01 carry it. `body_focus` DOES say "feet" — on
 * 278 items on ACCOUNT_ID — but it says so whenever feet are VISIBLE, not when they
 * are the subject, so a lingerie pose with her feet in frame matches. Hence a
 * 22% hit rate (307 candidates -> 68 keepers) and hence this screen: selling
 * straight off the tag would charge a fan for three photos he did not ask for
 * out of every four.
 *
 * TWO PASSES, and the first one is free. Roughly half the set is rejectable on
 * the DESCRIPTION alone — "walks away from the camera on a paved street" is not
 * a feet picture whatever the tag says — so every tile carries its description
 * and the keyboard works without ever waiting for a pixel.
 *
 * ⚠️ IMAGES ARE RATE-GATED ON PURPOSE. Tiles use /image (full frame), never
 * /thumb: the thumb is a 300x300 centre-crop of a 3:4 portrait and feet sit at
 * exactly the edge it discards — judging from the square means judging from less
 * of the picture than the vision model saw. But /image is a relay-side fetch,
 * the relay refetches at VAULT_STILL_CONCURRENCY=6, and a cold pane asking for
 * 280 at once is the documented route to fd exhaustion, which takes the DB and
 * the send lane down with it. So `useImageSlot` below holds a hard in-flight cap
 * and tiles queue for a turn.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";

import {
  mirrorFullSrc,
  publishFolderToOf,
  savePackTriage,
  usePackCandidates,
  type PackCandidate,
} from "@/hooks/useVaultCache";

/** Strictly below the relay's own VAULT_STILL_CONCURRENCY (6) so the pane can
 *  never be the thing that exhausts it, with headroom for the rest of the app. */
const MAX_INFLIGHT_IMAGES = 4;

/** Fan-facing meaning of each rung, so the operator is filing against what the
 *  caption will claim rather than against a folder name. These mirror SPEC 5.2's
 *  authored rung phrases. */
const RUNG_HELP: Record<string, string> = {
  tease: "covered — socks, stockings, heels, boots. Rides free as the preview.",
  nude: "bare feet, body NOT nude. This is the product.",
  "nude-body": "bare feet AND her body nude. Never the first ask.",
};

const RUNG_STYLE: Record<string, string> = {
  tease: "border-sky-500/60 bg-sky-500/15 text-sky-300",
  nude: "border-emerald-500/60 bg-emerald-500/15 text-emerald-300",
  "nude-body": "border-fuchsia-500/60 bg-fuchsia-500/15 text-fuchsia-300",
};

/** A module-wide gate: at most `limit` tiles hold a slot at once, FIFO after
 *  that. A slot is identified by the grant callback it was handed out to, so
 *  release is exact — releasing a token that is still queued cancels the queue
 *  entry instead of wrongly freeing someone else's slot. */
function makeSlotGate(limit: number) {
  let inFlight = 0;
  const waiting: (() => void)[] = [];
  return {
    acquire(grant: () => void) {
      if (inFlight < limit) {
        inFlight++;
        grant();
        return;
      }
      waiting.push(grant);
    },
    release(grant: () => void) {
      const queued = waiting.indexOf(grant);
      if (queued >= 0) {
        waiting.splice(queued, 1); // still waiting — never held a slot
        return;
      }
      inFlight = Math.max(0, inFlight - 1);
      const next = waiting.shift();
      if (next) {
        inFlight++;
        next();
      }
    },
  };
}

const imageGate = makeSlotGate(MAX_INFLIGHT_IMAGES);

/**
 * A src, once this tile is both on screen and holding a slot — plus the
 * `settle` the <img> MUST call when it loads or errors.
 *
 * ⚠️ Settling is not optional. A tile that renders its image and keeps the slot
 * starves every tile behind it: with a cap of 4 the pane would show four photos
 * and then stop forever. The slot is released on load, on error, and on unmount,
 * and the token is nulled so none of those can double-release and inflate the cap.
 */
function useImageSlot(src: string, visible: boolean): { src: string | null; settle: () => void } {
  const [granted, setGranted] = useState(false);
  const tokenRef = useRef<(() => void) | null>(null);

  const settle = useCallback(() => {
    const token = tokenRef.current;
    if (!token) return;
    tokenRef.current = null;
    imageGate.release(token);
  }, []);

  useEffect(() => {
    if (!visible) return;
    const grant = () => setGranted(true);
    tokenRef.current = grant;
    imageGate.acquire(grant);
    return settle; // unmounting (or re-keying) hands the slot back
  }, [visible, src, settle]);

  return { src: granted ? src : null, settle };
}

function Tile({
  accountId,
  item,
  rung,
  rungs,
  onRule,
}: {
  accountId: string;
  item: PackCandidate;
  rung: string | null;
  rungs: string[];
  onRule: (mediaId: number, rung: string | null) => void;
}) {
  const ref = useRef<HTMLDivElement | null>(null);
  const [visible, setVisible] = useState(false);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    const el = ref.current;
    if (!el || visible) return;
    // rootMargin gives the gate a head start so scrolling doesn't stall on a
    // cold tile, without ever asking for the whole set at once.
    const io = new IntersectionObserver(
      (entries) => entries.forEach((e) => e.isIntersecting && setVisible(true)),
      { rootMargin: "400px" },
    );
    io.observe(el);
    return () => io.disconnect();
  }, [visible]);

  const [failed, setFailed] = useState(false);
  const { src, settle } = useImageSlot(mirrorFullSrc(accountId, item.media_id), visible);
  const isVideo = item.kind !== "photo";

  // Load AND error both settle: either way this tile is done with the network
  // and the next one in the queue must be let through. A still that 404s is
  // ordinary (the cache is built lazily), so it gets a placeholder rather than
  // the browser's broken-image glyph — one cold item should not read as a
  // broken page, and the description is still enough to rule on.
  const onLoad = () => {
    setLoaded(true);
    settle();
  };
  const onError = () => {
    setFailed(true);
    setLoaded(true);
    settle();
  };

  return (
    <div
      ref={ref}
      className={[
        "flex flex-col rounded-lg border overflow-hidden bg-bg-subtle",
        rung ? RUNG_STYLE[rung] ?? "border-fg-dim" : "border-border",
      ].join(" ")}
    >
      <div className="relative aspect-[3/4] bg-black/40">
        {src && !failed ? (
          <img
            src={src}
            alt=""
            loading="lazy"
            onLoad={onLoad}
            onError={onError}
            className={[
              "h-full w-full object-contain transition-opacity",
              loaded ? "opacity-100" : "opacity-0",
            ].join(" ")}
          />
        ) : null}
        {(!loaded || failed) && (
          <div className="absolute inset-0 grid place-items-center px-2 text-center text-[10px] text-fg-dim">
            {failed ? "no preview — judge from the text" : visible ? "loading…" : ""}
          </div>
        )}
        {isVideo && (
          <span className="absolute left-1 top-1 rounded bg-black/70 px-1 text-[10px] text-white">
            {item.kind}
          </span>
        )}
        {rung && (
          <span className="absolute right-1 top-1 rounded bg-black/70 px-1 text-[10px] text-white">
            {rung}
          </span>
        )}
      </div>

      {/* Pass A: reject on text alone, no pixels needed. */}
      <p className="line-clamp-3 px-2 py-1 text-[10px] leading-snug text-fg-dim">
        {item.description || "—"}
      </p>

      {/* The reject takes only its glyph's width so the three rung labels get
          the rest — at 8 columns they truncate to nonsense otherwise. */}
      <div className="mt-auto grid grid-cols-[1fr_1fr_1fr_auto] gap-px bg-border p-px text-[10px]">
        {rungs.map((r) => (
          <button
            key={r}
            type="button"
            onClick={() => onRule(item.media_id, rung === r ? null : r)}
            title={RUNG_HELP[r] ?? r}
            className={[
              "whitespace-nowrap px-1 py-1.5 transition-colors",
              rung === r ? "bg-accent text-white font-medium" : "bg-bg hover:bg-bg-hover",
            ].join(" ")}
          >
            {/* "body" not "nude-body": at this width the full name truncates to
                nonsense, and tease / nude / body reads as the ladder it is. The
                title carries the precise meaning. */}
            {r === "nude-body" ? "body" : r}
          </button>
        ))}
        <button
          type="button"
          onClick={() => onRule(item.media_id, null)}
          title="Not this category. Nothing is stored about a rejection."
          className={[
            "whitespace-nowrap px-1 py-1.5 transition-colors",
            rung === null ? "bg-bg-hover text-fg-dim" : "bg-bg hover:bg-rose-500/20",
          ].join(" ")}
        >
          ✕
        </button>
      </div>
    </div>
  );
}

export default function VaultPackPickerModal({
  accountId,
  category = "feet",
  onClose,
}: {
  accountId: string;
  category?: string;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const { data, isLoading, error } = usePackCandidates(accountId, category);

  /** Unsaved verdicts, media_id -> rung|null. Absent = untouched this session. */
  const [draft, setDraft] = useState<Record<number, string | null>>({});
  const [filter, setFilter] = useState("undecided");
  /** Non-empty while any request is in flight; shown once, in the status bar. */
  const [busy, setBusy] = useState("");
  const [note, setNote] = useState("");

  const rungs = data?.rungs ?? [];
  // The shelf comes from the QUERY, not from the last save's response, so the
  // rungs and their publish buttons are there the moment the modal opens.
  const shelf = data?.folders ?? [];

  /** The saved rung overlaid with anything unsaved. */
  const effective = useCallback(
    (item: PackCandidate): string | null =>
      item.media_id in draft ? draft[item.media_id] : item.rung,
    [draft],
  );

  const shown = useMemo(() => {
    const all = data?.candidates ?? [];
    if (filter === "all") return all;
    if (filter === "undecided") {
      // Undecided means "never ruled on" — an explicit reject is a decision and
      // drops out, which is what makes the first pass converge.
      return all.filter((c) => effective(c) === null && !(c.media_id in draft));
    }
    return all.filter((c) => effective(c) === filter);
  }, [data, filter, draft, effective]);

  const tally = useMemo(() => {
    const out: Record<string, number> = {};
    for (const c of data?.candidates ?? []) {
      const r = effective(c);
      if (r) out[r] = (out[r] ?? 0) + 1;
    }
    return out;
  }, [data, effective]);

  const dirty = Object.keys(draft).length;

  function rule(mediaId: number, rung: string | null) {
    setDraft((d) => ({ ...d, [mediaId]: rung }));
  }

  async function save() {
    if (!dirty) return;
    setBusy("saving…");
    setNote("");
    try {
      const verdicts = Object.entries(draft).map(([media_id, rung]) => ({
        media_id: Number(media_id),
        rung,
      }));
      const res = await savePackTriage(accountId, category, verdicts);
      setDraft({});
      setNote(`saved ${res.saved} (${res.rejected} rejected)`);
      await qc.invalidateQueries({ queryKey: ["vault-pack-candidates", accountId, category] });
      await qc.invalidateQueries({ queryKey: ["vault-internal-folders", accountId] });
    } finally {
      setBusy("");
    }
  }

  async function publish(folderId: number, name: string) {
    setBusy(`publishing ${name} to OF…`);
    setNote("");
    try {
      const res = await publishFolderToOf(accountId, folderId);
      setNote(
        `${res.name} → OF list ${res.of_list_id}: pushed ${res.pushed}` +
          (res.stale_on_of.length
            ? ` · ⚠ ${res.stale_on_of.length} item(s) still on OF that this folder no longer holds (clearing them needs a delete+recreate)`
            : " · exact copy"),
      );
      await qc.invalidateQueries({ queryKey: ["vault-pack-candidates", accountId, category] });
    } catch (e) {
      setNote(`publish failed: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setBusy("");
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4">
      <div className="flex h-full w-full max-w-7xl flex-col rounded-xl border border-border bg-bg shadow-xl">
        <header className="flex flex-wrap items-center gap-3 border-b border-border px-4 py-3">
          <h2 className="text-sm font-semibold">
            Pack picker — {category}
          </h2>
          {/* Live tally — INCLUDES unsaved draft, which is why it is labelled.
              The shelf below shows the SAVED counts, and the two legitimately
              disagree until Save. */}
          <span className="text-xs text-fg-dim">
            {data ? `${data.candidates.length} candidates · decided` : "…"}
            {rungs.map((r) => ` ${r} ${tally[r] ?? 0}`).join(" ·")}
          </span>

          <div className="ml-auto flex items-center gap-2">
            {["undecided", ...rungs, "all"].map((f) => (
              <button
                key={f}
                type="button"
                onClick={() => setFilter(f)}
                className={[
                  "rounded px-2 py-1 text-xs",
                  filter === f ? "bg-accent text-white" : "bg-bg-subtle hover:bg-bg-hover",
                ].join(" ")}
              >
                {f}
              </button>
            ))}
            <button
              type="button"
              onClick={save}
              disabled={!dirty || !!busy}
              className="rounded bg-emerald-600 px-3 py-1 text-xs font-medium text-white disabled:opacity-40"
            >
              {dirty ? `Save ${dirty}` : "Saved"}
            </button>
            <button type="button" onClick={onClose} className="px-2 text-fg-dim hover:text-fg">
              ✕
            </button>
          </div>
        </header>

        {/* The shelf, always — publishing must not be reachable only in the
            moment after a save. */}
        <div className="flex flex-wrap items-center gap-3 border-b border-border bg-bg-subtle px-4 py-2 text-xs">
          {shelf.map((f) => (
            <span key={f.name} className="flex items-center gap-1">
              <b>{f.name}</b> {f.count} saved
              {f.of_list_id ? <span className="text-fg-dim">· on OF</span> : null}
              <button
                type="button"
                onClick={() => f.folder_id && publish(f.folder_id, f.name)}
                disabled={!f.folder_id || !f.count || !!busy}
                title="Create this as a REAL folder in her OnlyFans vault"
                className="rounded bg-bg px-1.5 py-0.5 hover:bg-bg-hover disabled:opacity-40"
              >
                {f.of_list_id ? "re-publish" : "publish to OF"}
              </button>
            </span>
          ))}
          <span className="ml-auto text-fg-dim">{busy || note}</span>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto p-3">
          {isLoading && <p className="p-6 text-center text-sm text-fg-dim">loading candidates…</p>}
          {error && (
            <p className="p-6 text-center text-sm text-rose-400">
              {error instanceof Error ? error.message : "failed to load"}
            </p>
          )}
          {data && shown.length === 0 && (
            <p className="p-6 text-center text-sm text-fg-dim">
              {filter === "undecided"
                ? "Nothing left undecided. Switch to a rung to review what you filed."
                : "Nothing here."}
            </p>
          )}
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-4 lg:grid-cols-6 xl:grid-cols-8">
            {shown.map((c) => (
              <Tile
                key={c.media_id}
                accountId={accountId}
                item={c}
                rung={effective(c)}
                rungs={rungs}
                onRule={rule}
              />
            ))}
          </div>
        </div>

        <footer className="border-t border-border px-4 py-2 text-[11px] text-fg-dim">
          Tiles show the FULL frame, not the square thumb — the crop cuts off exactly
          where feet are. Images load a few at a time on purpose. Nothing is written
          until you press Save, and nothing is sent to OnlyFans until you press
          “publish to OF”.
        </footer>
      </div>
    </div>
  );
}
