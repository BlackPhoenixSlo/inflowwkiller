"use client";

/**
 * Vault import — drop files in, or paste public Google Drive links, and the
 * relay pushes them into the OnlyFans vault.
 *
 * WHAT THE UI HAS TO BE HONEST ABOUT, because the underlying operation is slow
 * and strange:
 *
 *  - OnlyFans has no "save to vault" API. Each NEW file is attached to a
 *    far-future scheduled post, its vault id read off that post, and the post
 *    deleted. Nothing is ever published and no fan sees anything — but the
 *    import IS creating and deleting real objects, so the panel says so rather
 *    than pretending it is a plain upload.
 *  - OnlyFans throttles WRITES to ~1 per 10s and each new file costs two (the
 *    carrier post and its deletion), so a batch of NEW files runs at roughly
 *    3 a minute no matter how small they are or how fast the connection. Files
 *    already in the vault cost no post and go straight through, which is why a
 *    re-import finishes in seconds.
 *  - Big video is re-encoded first (OnlyFans rejects large objects outright),
 *    which can take minutes per file before a single byte is uploaded.
 *
 * So the panel reports phase and per-item state from the server's own run
 * record, and polls THAT RUN by id — the id comes back from the start call, so
 * the panel can never end up watching the previous import. State lives on the
 * relay, not here: a reload, a second tab, or a closed laptop does not lose the
 * run.
 */

import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { useVaultAccount, useVaultAccountId } from "@/components/vault/VaultAccount";
import { invalidateVaultGrid } from "@/hooks/useVaultCache";
import { fetchAllVaultLists, vaultListsKey } from "@/hooks/useVaultMedia";
import { relay, type VaultList } from "@/lib/relay";

const POLL_MS = 2500;
/** How long to keep polling after a start even if the server has not yet said
 *  `running` — the worker begins on a thread and the first poll can beat it. */
const FORCE_POLL_MS = 20_000;
/** Past this, a finished run's error message is history, not news. */
const ERROR_FRESH_MS = 24 * 60 * 60 * 1000;

type ItemStatus = "pending" | "uploading" | "done" | "failed" | "skipped";
/** The run's phases — `vault_upload.RUN_PHASES` on the wire. A union rather
 *  than `string`, because `runSummary` branches on two of these literals: with
 *  `phase?: string` a rename on the relay type-checks here and quietly turns
 *  the "caching previews" line back into "uploading", which is the one label
 *  whose entire job is to stop a real wait reading as a stall. */
type RunPhase = "collecting" | "uploading" | "mirroring" | "finished";

interface RunItem {
  name: string;
  source: "local" | "gdrive";
  size: number | null;
  status: ItemStatus;
  error: string | null;
  vault_id: number | null;
  deduped: boolean | null;
  hidden?: boolean | null;
  /** The vault id is real and permanent, but OnlyFans had not finished the
   *  encode when the carrier went away. It finishes on its own. */
  still_transcoding?: boolean | null;
  compressed_from?: number | null;
}

interface RunStatus {
  running: boolean;
  run_id?: string;
  status?: "running" | "done" | "failed" | "interrupted";
  phase?: RunPhase;
  error?: string | null;
  /** Foldering failed but the media IS in the vault — a warning, not a failure. */
  filing_error?: string | null;
  finished_at?: string | null;
  total?: number;
  done?: number;
  failed?: number;
  skipped?: number;
  /** Carriers we know the id of that could NOT be deleted — the relay's own
   *  name for them, so the wire and the module say the same word. */
  carriers_undeletable?: number;
  /** Carriers that may exist but were never recorded — the create call's
   *  response was lost. The sweep hunts these by their marker. */
  carriers_unrecorded?: number;
  items?: RunItem[];
}

const MB = 1024 * 1024;

function fmtSize(bytes: number | null | undefined): string {
  if (!bytes) return "";
  return bytes >= MB ? `${(bytes / MB).toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1024))} KB`;
}

/** Plain-language state for one row. Exported for tests. */
export function itemLabel(it: RunItem): string {
  if (it.status === "done" && it.deduped) return "already in vault";
  if (it.status === "done" && it.still_transcoding)
    return `in vault · ${it.vault_id ?? ""} · still processing`;
  if (it.status === "done") return `in vault · ${it.vault_id ?? ""}`;
  if (it.status === "uploading") return "uploading…";
  if (it.status === "pending") return "waiting";
  if (it.status === "skipped") return it.error || "skipped";
  return it.error || "failed";
}

/** One line summarising a run, in the words an operator would use. */
export function runSummary(s: RunStatus): string {
  if (!s.run_id) return "No import has run yet.";
  const done = s.done ?? 0;
  const total = s.total ?? 0;
  if (s.running) {
    // `mirroring` is the tail of the run: every file is in the vault and the
    // relay is reading them back one by one to fill the local mirror. It is
    // deliberately still `running` — the grid refreshes on the finish edge, and
    // refreshing before the mirror is written is what made a large import look
    // like it uploaded nothing. Naming it stops that wait reading as a stall.
    if (s.phase === "mirroring") return `${done} in vault — caching previews`;
    const phase = s.phase === "collecting" ? "fetching files" : "uploading";
    return `${phase} — ${done} of ${total} done`;
  }
  if (s.status === "interrupted") return "Interrupted — the relay restarted mid-run. Re-run to finish.";
  const bits = [`${done} in vault`];
  if (s.failed) bits.push(`${s.failed} failed`);
  if (s.skipped) bits.push(`${s.skipped} skipped`);
  return bits.join(" · ");
}

/** Is this run's error still worth showing? A failed run's message used to sit
 *  on the panel on every page load until somebody happened to import again. */
export function showsError(s: RunStatus, now: number = Date.now()): boolean {
  if (!s.error) return false;
  if (s.status !== "failed" && s.status !== "interrupted") return false;
  if (!s.finished_at) return true;
  const at = Date.parse(s.finished_at);
  return Number.isNaN(at) || now - at < ERROR_FRESH_MS;
}

export default function VaultImportCard() {
  const qc = useQueryClient();
  // The account every call on this card is scoped to — the PAGE's model, from
  // the header picker. It used to be `accounts[0]`, unconditionally: switching
  // model moved the grid and left the importer behind, so files uploaded into
  // the first account's vault (and were offered the first account's folder ids,
  // which go to OnlyFans verbatim) while the operator watched a different
  // model's vault above the form.
  //
  // A plain string, and the card is REMOUNTED when it changes — the same
  // mechanism the grid and the review tab use. It used to clear itself with an
  // effect instead, which fired once on the null → id transition every mount
  // and silently binned files an operator had staged while `/admin/accounts`
  // was still answering.
  const accountId = useVaultAccountId();
  const { account, sessionLost } = useVaultAccount();
  const ctx = { accountId };
  const fileRef = useRef<HTMLInputElement>(null);
  const [picked, setPicked] = useState<File[]>([]);
  const [links, setLinks] = useState("");
  const [startError, setStartError] = useState<string | null>(null);
  const [runId, setRunId] = useState<string | null>(null);
  const [folder, setFolder] = useState<string>("");
  const settledRef = useRef(false);
  const forceUntilRef = useRef(0);

  // Vault folders, so "file everything into a folder" is reachable from the UI
  // the feature was built for. Same paginated fetcher every other reader of
  // this cache uses — a single-page queryFn under this key would poison it.
  const folders = useQuery({
    queryKey: vaultListsKey(accountId),
    queryFn: () => fetchAllVaultLists(ctx),
    staleTime: 5 * 60 * 1000,
    select: (d: { list?: VaultList[] }) =>
      (d.list ?? []).filter((l) => l.type === "custom"),
  });

  const status = useQuery<RunStatus>({
    // Per MODEL as well as per run: with no `run_id` this route answers with the
    // latest run for whatever account the header names, so a key without the
    // account served one model's import progress under another's name.
    queryKey: ["vault-import-status", accountId, runId],
    queryFn: () =>
      relay.get<RunStatus>(
        runId
          ? `/api/of/v2/vault/upload/status?run_id=${encodeURIComponent(runId)}`
          : "/api/of/v2/vault/upload/status",
        ctx,
      ),
    // Self-arming: poll while the run is live, and unconditionally for a short
    // window after a start — the route creates the record before it answers,
    // but the worker reaches its first item on a thread a moment later.
    refetchInterval: (q) =>
      q.state.data?.running || Date.now() < forceUntilRef.current ? POLL_MS : false,
    staleTime: 0,
    refetchOnWindowFocus: true,
  });

  const s = status.data ?? { running: false };
  const running = !!s.running;

  // When a run finishes, the vault grid is stale — it has new media in it.
  useEffect(() => {
    if (running) {
      settledRef.current = true;
      return;
    }
    if (settledRef.current) {
      settledRef.current = false;
      // Every key the grid, the folder rail and the counter read — as ONE
      // call, built from the hooks' own key factories. Hand-listing them is how
      // this went wrong twice: `["vault"]` looks like a prefix of
      // `["vault-media", …]` but TanStack compares elements, so it matched
      // nothing at all and an import stayed invisible behind a three-day
      // `vault-media` cache; and the panel's own copy of the list was missing a
      // key from the day it was written.
      invalidateVaultGrid(qc, accountId);
    }
  }, [running, qc, accountId]);

  const start = useMutation({
    mutationFn: async () => {
      const form = new FormData();
      for (const f of picked) form.append("files", f, f.name);
      if (links.trim()) form.append("drive_links", links.trim());
      if (folder) form.append("list_id", folder);
      return relay.uploadForm<{ run_id: string }>("/api/of/v2/vault/upload/batch", form, ctx);
    },
    onSuccess: (r) => {
      setPicked([]);
      setLinks("");
      if (fileRef.current) fileRef.current.value = "";
      setStartError(null);
      forceUntilRef.current = Date.now() + FORCE_POLL_MS;
      // Watch the run we just started, by id — not "whatever the latest run is".
      if (r?.run_id) setRunId(r.run_id);
      else void status.refetch();
    },
    onError: (e: unknown) => setStartError(e instanceof Error ? e.message : "Could not start the import"),
  });

  const nothingToDo = picked.length === 0 && !links.trim();
  const items = s.items ?? [];
  // The model this card writes to, named. Never a placeholder: this card does
  // not exist until the page knows which vault it is writing into.
  const modelName = account?.nickname || account?.id || accountId;

  return (
    <section className="rounded-lg border border-border bg-panel p-4 space-y-3">
      <header className="space-y-1">
        {/* WHOSE vault, said out loud. This card used to name no model at all
            while uploading into a different one than the page displayed, and
            the operator's only clue was recognising the media afterwards. An
            upload into the wrong creator's vault cannot be undone — OnlyFans'
            delete is a one-way hide — so the target is stated before the form,
            not inferred from a chip somewhere else on the page. */}
        <h2 className="text-sm font-semibold">
          Import to{" "}
          <span className="text-accent">{modelName}</span>
          &apos;s vault
        </h2>
        <p className="text-xs text-fg-dim">
          Files from this computer, or public Google Drive links (a folder link
          works). Large video is re-encoded before upload — OnlyFans refuses
          very large files. Nothing is posted or sent to fans.
        </p>
      </header>

      <div className="space-y-2">
        <input
          ref={fileRef}
          type="file"
          multiple
          accept="image/*,video/*,audio/*"
          disabled={running}
          onChange={(e) => setPicked(Array.from(e.target.files ?? []))}
          className="block w-full text-xs file:mr-3 file:rounded file:border-0 file:bg-bg file:px-3 file:py-1.5 file:text-xs file:text-fg disabled:opacity-50"
        />
        {picked.length > 0 && (
          <p className="text-xs text-fg-dim">
            {picked.length} file{picked.length === 1 ? "" : "s"} ·{" "}
            {fmtSize(picked.reduce((n, f) => n + f.size, 0))}
          </p>
        )}

        <textarea
          value={links}
          disabled={running}
          onChange={(e) => setLinks(e.target.value)}
          rows={2}
          placeholder="https://drive.google.com/drive/folders/…  (one per line)"
          className="w-full rounded border border-border bg-bg px-2 py-1.5 text-xs disabled:opacity-50"
        />

        <label className="flex items-center gap-2 text-xs text-fg-dim">
          <span className="shrink-0">Put into folder</span>
          <select
            value={folder}
            disabled={running}
            onChange={(e) => setFolder(e.target.value)}
            className="min-w-0 flex-1 rounded border border-border bg-bg px-2 py-1 text-xs disabled:opacity-50"
          >
            <option value="">No folder — just the vault</option>
            {(folders.data ?? []).map((l) => (
              <option key={String(l.id)} value={String(l.id)}>
                {l.name}
              </option>
            ))}
          </select>
        </label>
      </div>

      <div className="flex items-center gap-3">
        <button
          type="button"
          // `sessionLost` is a hard stop, not a nicety. The relay builds this
          // account's OFClient before it claims the run, so a dead session is
          // now a clean 503 that strands nothing — but a start that cannot
          // possibly work, offered under a banner saying nothing on this page
          // can load, is an error message dressed as a button.
          disabled={running || nothingToDo || start.isPending || sessionLost}
          onClick={() => start.mutate()}
          className="rounded bg-accent px-3 py-1.5 text-xs font-semibold text-white disabled:opacity-40"
        >
          {running
            ? "Importing…"
            : sessionLost
              ? "Session dropped"
              : start.isPending
                ? "Starting…"
                : `Start import → ${modelName}`}
        </button>
        <span className="text-xs text-fg-dim">{runSummary(s)}</span>
      </div>

      {running && (
        <p className="text-xs text-fg-dim">
          Each new file costs two paced writes — the scheduled post that carries
          it, and the deletion of that post — and OnlyFans allows about one write
          every 10 seconds, so new files land at roughly 3 a minute; anything
          already in the vault is recognised and skipped straight away. You can
          leave this page — the import keeps going and this panel picks it back
          up.
        </p>
      )}

      {(startError || showsError(s)) && (
        <p className="text-xs text-err">{startError || s.error}</p>
      )}

      {s.filing_error && (
        <p className="text-xs text-warn">
          {s.filing_error} — the media is in the vault, just not in that folder.
        </p>
      )}

      {!!s.carriers_undeletable && (
        <p className="text-xs text-err">
          {s.carriers_undeletable} scheduled post{s.carriers_undeletable === 1 ? "" : "s"} could not be
          removed. Check Scheduled posts on OnlyFans — they publish on their date.
        </p>
      )}

      {!!s.carriers_unrecorded && (
        <p className="text-xs text-warn">
          {s.carriers_unrecorded} scheduled post
          {s.carriers_unrecorded === 1 ? " may have been" : "s may have been"} created
          without being recorded — the relay is searching OnlyFans for
          {s.carriers_unrecorded === 1 ? " it" : " them"} and will delete
          {s.carriers_unrecorded === 1 ? " it" : " them"}. If this is still here
          tomorrow, check Scheduled posts on OnlyFans yourself.
        </p>
      )}

      {items.length > 0 && (
        <ul className="max-h-56 overflow-y-auto divide-y divide-border text-xs">
          {items.map((it, i) => (
            <li key={`${it.name}-${i}`} className="flex items-baseline justify-between gap-3 py-1">
              <span className="truncate" title={it.name}>{it.name}</span>
              <span
                className={
                  it.status === "failed"
                    ? "shrink-0 text-err"
                    : it.status === "done"
                      ? "shrink-0 text-ok"
                      : "shrink-0 text-fg-dim"
                }
              >
                {it.compressed_from
                  ? `${fmtSize(it.compressed_from)} → ${fmtSize(it.size)} · `
                  : ""}
                {itemLabel(it)}
              </span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
